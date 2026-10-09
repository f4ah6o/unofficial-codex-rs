use std::collections::VecDeque;
use std::future::Future;
use std::io;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use codex_http_client::OutboundProxyRoute;
use codex_http_client::build_rustls_client_config_with_custom_ca;
use futures::StreamExt;
use futures::stream::FuturesUnordered;
use rustls::ClientConfig;
use rustls::pki_types::ServerName;
use tokio::net::TcpStream;
use tokio::time::Instant;
use tokio::time::sleep_until;
use tokio_rustls::TlsConnector;
use tokio_tungstenite::Connector;
use tokio_tungstenite::client_async_tls_with_config;
use tokio_tungstenite::connect_async_tls_with_config;
use tokio_tungstenite::tungstenite::Error as WebSocketError;
use tokio_tungstenite::tungstenite::error::TlsError;
use tokio_tungstenite::tungstenite::error::UrlError;
use tokio_tungstenite::tungstenite::handshake::client::Request;
use tokio_tungstenite::tungstenite::handshake::client::Response;
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;

use crate::AsyncIo;
use crate::ConnectionInner;
use crate::TcpNodelay;
use crate::proxy::ProxyConfig;
use crate::proxy::connect_via_proxy;
use crate::proxy::invalid_proxy_config;
use crate::proxy::no_proxy_bypasses;
use crate::proxy::proxy_config_from_env;

const HAPPY_EYEBALLS_DELAY: Duration = Duration::from_millis(250);

pub(crate) async fn connect(
    request: Request,
    config: WebSocketConfig,
    tls_config: Option<Arc<ClientConfig>>,
    proxy_route: OutboundProxyRoute,
    tcp_nodelay: TcpNodelay,
) -> Result<(ConnectionInner, Response), WebSocketError> {
    let disable_nagle = tcp_nodelay == TcpNodelay::Enabled;
    let proxy_url = match proxy_route {
        OutboundProxyRoute::TransportDefault => {
            // Mirror the forked tungstenite dialer: environment proxy variables
            // (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY) select a CONNECT/SOCKS5
            // tunnel; without them the stock connect path handles the socket itself.
            if let Some(proxy) = proxy_config_from_env(request.uri())? {
                let host = websocket_host(&request)?;
                let port = websocket_port(&request)?;
                let stream = connect_tcp(proxy.authority(), tcp_nodelay)
                    .await
                    .map_err(WebSocketError::Io)?;
                let stream = connect_via_proxy(stream, &proxy, host, port).await?;
                let (stream, response) = client_async_tls_with_config(
                    request,
                    stream,
                    Some(config),
                    tls_config.map(Connector::Rustls),
                )
                .await?;
                return Ok((ConnectionInner::TransportDefault(stream), response));
            }
            let (stream, response) = connect_async_tls_with_config(
                request,
                Some(config),
                disable_nagle,
                tls_config.map(Connector::Rustls),
            )
            .await?;
            return Ok((ConnectionInner::TransportDefault(stream), response));
        }
        OutboundProxyRoute::Direct => None,
        OutboundProxyRoute::Proxy { url, no_proxy } => {
            // Apply the resolved NO_PROXY value at this layer: the upstream tungstenite
            // fork made the same decision through its environment parser.
            match no_proxy {
                Some(no_proxy)
                    if no_proxy_bypasses(
                        websocket_host(&request)?,
                        websocket_port(&request)?,
                        &no_proxy,
                    ) =>
                {
                    None
                }
                _ => Some(url),
            }
        }
    };

    let stream: Box<dyn AsyncIo> = match proxy_url {
        None => {
            let host = websocket_host(&request)?;
            let port = websocket_port(&request)?;
            Box::new(
                connect_tcp(host_port(host, port), tcp_nodelay)
                    .await
                    .map_err(WebSocketError::Io)?,
            )
        }
        Some(url) => {
            let proxy = ProxyEndpoint::parse(&url)?;
            let host = websocket_host(&request)?;
            let port = websocket_port(&request)?;
            let stream = connect_tcp(proxy.config.authority(), tcp_nodelay)
                .await
                .map_err(WebSocketError::Io)?;
            let stream: Box<dyn AsyncIo> = if proxy.tls {
                let proxy_tls_config = match &tls_config {
                    Some(tls_config) => Arc::clone(tls_config),
                    None => build_rustls_client_config_with_custom_ca()
                        .map_err(|error| WebSocketError::Io(error.into()))?,
                };
                let server_name = ServerName::try_from(proxy.config.host.clone())
                    .map_err(|_| WebSocketError::Tls(TlsError::InvalidDnsName))?;
                let stream = TlsConnector::from(proxy_tls_config)
                    .connect(server_name, stream)
                    .await
                    .map_err(WebSocketError::Io)?;
                Box::new(stream)
            } else {
                Box::new(stream)
            };
            connect_via_proxy(stream, &proxy.config, host, port).await?
        }
    };

    let (stream, response) = client_async_tls_with_config(
        request,
        stream,
        Some(config),
        tls_config.map(Connector::Rustls),
    )
    .await?;
    Ok((ConnectionInner::Routed(stream), response))
}

#[derive(Debug, PartialEq, Eq)]
struct ProxyEndpoint {
    config: ProxyConfig,
    tls: bool,
}

impl ProxyEndpoint {
    fn parse(url: &str) -> Result<Self, WebSocketError> {
        let mut parsed_url =
            url::Url::parse(url).map_err(|_| invalid_proxy_config("<redacted>"))?;
        let tls = parsed_url.scheme() == "https";
        if tls {
            // Capture the HTTPS default before changing schemes: `Url` normalizes default ports,
            // so setting 443 before rewriting to HTTP would discard it and later imply port 80.
            let port = parsed_url
                .port_or_known_default()
                .ok_or_else(|| invalid_proxy_config("<redacted>"))?;
            parsed_url
                .set_scheme("http")
                .map_err(|_| invalid_proxy_config("<redacted>"))?;
            parsed_url
                .set_port(Some(port))
                .map_err(|_| invalid_proxy_config("<redacted>"))?;
        }
        let config = ProxyConfig::parse(parsed_url.as_str()).map_err(|error| match error {
            WebSocketError::Url(UrlError::UnsupportedUrlScheme) => error,
            _ => invalid_proxy_config("<redacted>"),
        })?;
        Ok(Self { config, tls })
    }
}

fn websocket_host(request: &Request) -> Result<&str, WebSocketError> {
    request
        .uri()
        .host()
        .ok_or(WebSocketError::Url(UrlError::NoHostName))
}

fn websocket_port(request: &Request) -> Result<u16, WebSocketError> {
    request
        .uri()
        .port_u16()
        .or_else(|| match request.uri().scheme_str() {
            Some("ws") => Some(80),
            Some("wss") => Some(443),
            _ => None,
        })
        .ok_or(WebSocketError::Url(UrlError::UnsupportedUrlScheme))
}

fn host_port(host: &str, port: u16) -> String {
    if host.contains(':') && !host.starts_with('[') {
        format!("[{host}]:{port}")
    } else {
        format!("{host}:{port}")
    }
}

async fn connect_tcp(address: String, tcp_nodelay: TcpNodelay) -> io::Result<TcpStream> {
    let addresses = tokio::net::lookup_host(address).await?.collect::<Vec<_>>();
    let stream = connect_happy_eyeballs(addresses, TcpStream::connect).await?;
    if tcp_nodelay == TcpNodelay::Enabled {
        stream.set_nodelay(/*nodelay*/ true)?;
    }
    Ok(stream)
}

async fn connect_happy_eyeballs<T, F, Fut>(
    addresses: Vec<SocketAddr>,
    mut connect: F,
) -> io::Result<T>
where
    F: FnMut(SocketAddr) -> Fut,
    Fut: Future<Output = io::Result<T>>,
{
    let mut addresses = addresses.into_iter();
    let Some(first_address) = addresses.next() else {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "could not resolve to any address",
        ));
    };

    let first_is_ipv4 = first_address.is_ipv4();
    let mut preferred = VecDeque::new();
    let mut alternate = VecDeque::new();
    for address in addresses {
        if address.is_ipv4() == first_is_ipv4 {
            preferred.push_back(address);
        } else {
            alternate.push_back(address);
        }
    }

    let mut addresses = VecDeque::new();
    while !preferred.is_empty() || !alternate.is_empty() {
        if let Some(address) = alternate.pop_front() {
            addresses.push_back(address);
        }
        if let Some(address) = preferred.pop_front() {
            addresses.push_back(address);
        }
    }

    let mut attempts = FuturesUnordered::new();
    attempts.push(connect(first_address));
    let mut next_attempt_at = Instant::now() + HAPPY_EYEBALLS_DELAY;
    let mut last_error = None;

    loop {
        if addresses.is_empty() {
            match attempts.next().await {
                Some(Ok(stream)) => return Ok(stream),
                Some(Err(error)) => {
                    if attempts.is_empty() {
                        return Err(error);
                    }
                    last_error = Some(error);
                }
                None => {
                    return Err(last_error.unwrap_or_else(|| {
                        io::Error::other("connection attempts ended without an error")
                    }));
                }
            }
            continue;
        }

        tokio::select! {
            result = attempts.next() => {
                match result {
                    Some(Ok(stream)) => return Ok(stream),
                    Some(Err(error)) => {
                        last_error = Some(error);
                        let address = take_next_address(&mut addresses)?;
                        attempts.push(connect(address));
                        next_attempt_at = Instant::now() + HAPPY_EYEBALLS_DELAY;
                    }
                    None => {
                        let address = take_next_address(&mut addresses)?;
                        attempts.push(connect(address));
                        next_attempt_at = Instant::now() + HAPPY_EYEBALLS_DELAY;
                    }
                }
            }
            _ = sleep_until(next_attempt_at) => {
                let address = take_next_address(&mut addresses)?;
                attempts.push(connect(address));
                next_attempt_at = Instant::now() + HAPPY_EYEBALLS_DELAY;
            }
        }
    }
}

fn take_next_address(addresses: &mut VecDeque<SocketAddr>) -> io::Result<SocketAddr> {
    addresses
        .pop_front()
        .ok_or_else(|| io::Error::other("connection address queue unexpectedly empty"))
}

#[cfg(test)]
#[path = "dialer_tests.rs"]
mod tests;
