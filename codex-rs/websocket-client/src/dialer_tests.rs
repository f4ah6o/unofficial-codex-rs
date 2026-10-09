use std::net::SocketAddr;
use std::process::Command;
use std::sync::Arc;
use std::time::Duration;

use codex_http_client::HttpClientFactory;
use codex_http_client::OutboundProxyPolicy;
use codex_http_client::OutboundProxyRoute;
use codex_utils_rustls_provider::ensure_rustls_crypto_provider;
use futures::SinkExt;
use futures::StreamExt;
use pretty_assertions::assert_eq;
use rcgen::CertifiedKey;
use rcgen::generate_simple_self_signed;
use rustls::ClientConfig;
use rustls::RootCertStore;
use rustls::ServerConfig;
use rustls::pki_types::CertificateDer;
use rustls::pki_types::PrivateKeyDer;
use rustls::pki_types::PrivatePkcs8KeyDer;
use tokio::net::TcpListener;
use tokio::task::JoinHandle;
use tokio_rustls::TlsAcceptor;
use tokio_tungstenite::MaybeTlsStream;
use tokio_tungstenite::accept_async;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;

use super::*;
use crate::AsyncIo;
use crate::WebSocketConnection;
use crate::WebSocketConnector;
use crate::WebSocketTlsMode;

#[tokio::test]
async fn public_connector_uses_factory_and_exposes_stream_and_sink() {
    let (target_addr, target_task) = start_echo_websocket_server(/*acceptor*/ None).await;
    let request = format!("ws://localhost:{}/v1/responses", target_addr.port())
        .into_client_request()
        .expect("websocket request should build");
    let factory = HttpClientFactory::new(OutboundProxyPolicy::ReqwestDefault);
    let connector = WebSocketConnector::new(&factory).expect("connector should build");

    let (mut websocket, _) = connector
        .connect(request, WebSocketConfig::default())
        .await
        .expect("websocket handshake should succeed");
    assert!(!websocket_tcp_nodelay(&websocket));
    let expected = Message::Text("hello".into());
    websocket
        .send(expected.clone())
        .await
        .expect("websocket should send");
    let actual = websocket
        .next()
        .await
        .expect("websocket should receive a message")
        .expect("websocket message should be valid");
    assert_eq!(actual, expected);

    target_task.await.expect("target task should finish");
}

#[tokio::test]
async fn public_connector_enables_tcp_nodelay_when_requested() {
    let (target_addr, target_task) = start_echo_websocket_server(/*acceptor*/ None).await;
    let request = format!("ws://localhost:{}/v1/responses", target_addr.port())
        .into_client_request()
        .expect("websocket request should build");
    let factory = HttpClientFactory::new(OutboundProxyPolicy::ReqwestDefault);
    let connector = WebSocketConnector::new(&factory)
        .expect("connector should build")
        .with_tcp_nodelay();

    let (mut websocket, _) = connector
        .connect(request, WebSocketConfig::default())
        .await
        .expect("websocket handshake should succeed");
    assert!(websocket_tcp_nodelay(&websocket));
    let expected = Message::Text("latency-sensitive".into());
    websocket
        .send(expected.clone())
        .await
        .expect("websocket should send");
    assert_eq!(
        websocket
            .next()
            .await
            .expect("websocket should receive a message")
            .expect("websocket message should be valid"),
        expected
    );

    target_task.await.expect("target task should finish");
}

#[tokio::test]
async fn tungstenite_default_tls_mode_ignores_invalid_custom_ca_in_a_subprocess() {
    let (target_addr, target_task) = start_echo_websocket_server(/*acceptor*/ None).await;
    let target_url = format!("ws://127.0.0.1:{}/v1/responses", target_addr.port());
    let executable = std::env::current_exe().expect("test executable should be available");
    let output = tokio::task::spawn_blocking(move || {
        let mut command = Command::new(executable);
        command.args([
            "--exact",
            "dialer::tests::tungstenite_default_tls_mode_subprocess_probe",
            "--nocapture",
        ]);
        for key in [
            "HTTP_PROXY",
            "http_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
            "SSL_CERT_FILE",
        ] {
            command.env_remove(key);
        }
        command
            .env(
                "CODEX_CA_CERTIFICATE",
                "/codex-websocket-client-nonexistent-custom-ca.pem",
            )
            .env("CODEX_WEBSOCKET_DEFAULT_TLS_PROBE_URL", target_url)
            .output()
            .expect("WebSocket default-TLS subprocess should run")
    })
    .await
    .expect("WebSocket default-TLS subprocess should join");

    assert!(
        output.status.success(),
        "WebSocket default-TLS subprocess failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    target_task.await.expect("target task should finish");
}

#[tokio::test]
async fn tungstenite_default_tls_mode_subprocess_probe() {
    let Ok(url) = std::env::var("CODEX_WEBSOCKET_DEFAULT_TLS_PROBE_URL") else {
        return;
    };
    let factory = HttpClientFactory::new(OutboundProxyPolicy::ReqwestDefault);
    assert!(
        WebSocketConnector::new(&factory).is_err(),
        "explicit Codex TLS should reject the invalid custom CA"
    );
    let connector =
        WebSocketConnector::new_with_tls_mode(&factory, WebSocketTlsMode::TungsteniteDefault)
            .expect("Tungstenite-default TLS should ignore custom CA configuration");
    let request = url
        .into_client_request()
        .expect("websocket request should build");
    let (mut websocket, _) = connector
        .connect(request, WebSocketConfig::default())
        .await
        .expect("WebSocket should connect without constructing Codex TLS");
    let expected = Message::Text("Tungstenite default TLS".into());
    websocket
        .send(expected.clone())
        .await
        .expect("WebSocket should send");
    assert_eq!(
        websocket
            .next()
            .await
            .expect("WebSocket should receive a message")
            .expect("WebSocket message should be valid"),
        expected
    );
}

#[tokio::test]
async fn direct_route_connects_secure_websocket() {
    let (tls_config, acceptor, _) = test_tls_configs();
    let (target_addr, target_task) = start_tls_websocket_server(acceptor).await;
    let request = format!("wss://localhost:{}/v1/responses", target_addr.port())
        .into_client_request()
        .expect("websocket request should build");

    let (inner, _) = connect(
        request,
        WebSocketConfig::default(),
        Some(tls_config),
        OutboundProxyRoute::Direct,
        TcpNodelay::Enabled,
    )
    .await
    .expect("direct websocket handshake should succeed");
    drop(WebSocketConnection { inner });

    target_task.await.expect("target task should finish");
}

#[tokio::test]
async fn proxy_route_is_rejected_with_registry_tungstenite() {
    let (_tls_config, acceptor, _) = test_tls_configs();
    let (target_addr, target_task) = start_tls_websocket_server(acceptor).await;
    let request = format!("wss://localhost:{}/v1/responses", target_addr.port())
        .into_client_request()
        .expect("websocket request should build");

    let result = connect(
        request,
        WebSocketConfig::default(),
        Some(_tls_config),
        OutboundProxyRoute::Proxy {
            url: "http://proxy.invalid:8080".to_string(),
            no_proxy: None,
        },
        TcpNodelay::Enabled,
    )
    .await;
    let Err(error) = result else {
        panic!("proxy routes should fail without the forked tungstenite dependency");
    };
    assert!(
        matches!(error, WebSocketError::Io(_)),
        "proxy route failure should surface as an I/O error: {error}"
    );

    drop(target_task);
}

#[tokio::test(start_paused = true)]
async fn happy_eyeballs_does_not_wait_for_stalled_preferred_family() {
    let stalled = "[2001:db8::1]:443"
        .parse::<SocketAddr>()
        .expect("stalled address should parse");
    let reachable = "127.0.0.1:443"
        .parse::<SocketAddr>()
        .expect("reachable address should parse");

    let connected = tokio::time::timeout(
        Duration::from_secs(1),
        connect_happy_eyeballs(vec![stalled, reachable], |address| async move {
            if address == stalled {
                std::future::pending::<()>().await;
            }
            Ok(address)
        }),
    )
    .await
    .expect("alternate family should start before timeout")
    .expect("alternate family should connect");

    assert_eq!(connected, reachable);
}

#[tokio::test]
async fn routed_tcp_connections_only_enable_nodelay_when_requested() {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("listener should bind");
    let address = listener
        .local_addr()
        .expect("listener should have an address")
        .to_string();

    let default_stream = connect_tcp(address.clone(), TcpNodelay::Default)
        .await
        .expect("default TCP connection should succeed");
    assert!(!default_stream.nodelay().expect("TCP_NODELAY should read"));

    let low_latency_stream = connect_tcp(address, TcpNodelay::Enabled)
        .await
        .expect("low-latency TCP connection should succeed");
    assert!(
        low_latency_stream
            .nodelay()
            .expect("TCP_NODELAY should read")
    );
}

fn websocket_tcp_nodelay(websocket: &WebSocketConnection) -> bool {
    let ConnectionInner::TransportDefault(stream) = &websocket.inner else {
        panic!("default connector should use Tungstenite's transport");
    };
    let MaybeTlsStream::Plain(stream) = stream.get_ref() else {
        panic!("test websocket should use a plain TCP stream");
    };
    stream.nodelay().expect("TCP_NODELAY should read")
}

async fn start_echo_websocket_server(
    acceptor: Option<TlsAcceptor>,
) -> (SocketAddr, JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("target listener should bind");
    let address = listener
        .local_addr()
        .expect("target listener should have an address");
    let task = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("target should accept");
        let stream: Box<dyn AsyncIo> = match acceptor {
            Some(acceptor) => Box::new(
                acceptor
                    .accept(stream)
                    .await
                    .expect("target TLS handshake should succeed"),
            ),
            None => Box::new(stream),
        };
        let mut websocket = accept_async(stream)
            .await
            .expect("target websocket handshake should succeed");
        let message = websocket
            .next()
            .await
            .expect("target should receive a message")
            .expect("target websocket message should be valid");
        websocket
            .send(message)
            .await
            .expect("target should echo the message");
    });
    (address, task)
}

async fn start_tls_websocket_server(acceptor: TlsAcceptor) -> (SocketAddr, JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("target listener should bind");
    let address = listener
        .local_addr()
        .expect("target listener should have an address");
    let task = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("target should accept");
        let stream = acceptor
            .accept(stream)
            .await
            .expect("target TLS handshake should succeed");
        let mut websocket = accept_async(stream)
            .await
            .expect("target websocket handshake should succeed");
        let _ = websocket.close(None).await;
    });
    (address, task)
}

fn test_tls_configs() -> (Arc<ClientConfig>, TlsAcceptor, CertificateDer<'static>) {
    ensure_rustls_crypto_provider();
    let CertifiedKey { cert, signing_key } =
        generate_simple_self_signed(vec!["localhost".to_string()])
            .expect("test certificate should generate");
    let certificate = cert.der().clone();
    let private_key = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(signing_key.serialize_der()));
    let server_config = ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(vec![certificate.clone()], private_key)
        .expect("test server config should build");

    let mut roots = RootCertStore::empty();
    roots
        .add(certificate.clone())
        .expect("test certificate should be trusted");
    let client_config = ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();

    (
        Arc::new(client_config),
        TlsAcceptor::from(Arc::new(server_config)),
        certificate,
    )
}
