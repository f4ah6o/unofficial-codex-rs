//! Proxy tunneling for WebSocket connections.
//!
//! This is a port of the `proxy` modules in the OpenAI `tokio-tungstenite`/`tungstenite`
//! forks: crates.io releases do not publish them, so the CONNECT/SOCKS5 handshakes and
//! `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY` resolution are reimplemented here.

use std::env;
use std::fmt;
use std::io;

use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64;
use tokio::io::AsyncRead;
use tokio::io::AsyncReadExt;
use tokio::io::AsyncWrite;
use tokio::io::AsyncWriteExt;
use tokio_tungstenite::tungstenite::Error as WebSocketError;
use tokio_tungstenite::tungstenite::error::UrlError;
use tokio_tungstenite::tungstenite::http::Uri;

const MAX_CONNECT_RESPONSE_SIZE: usize = 8192;

/// Proxy scheme supported by the tunnel implementation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ProxyScheme {
    /// HTTP CONNECT proxy.
    Http,
    /// SOCKS5 proxy with remote DNS resolution.
    Socks5,
    /// SOCKS5 proxy with local DNS resolution.
    Socks5h,
}

/// Proxy authentication credentials.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct ProxyAuth {
    /// Username for basic or SOCKS5 auth.
    pub username: String,
    /// Password for basic or SOCKS5 auth.
    pub password: String,
}

/// Resolved proxy configuration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct ProxyConfig {
    /// Proxy scheme.
    pub scheme: ProxyScheme,
    /// Proxy host.
    pub host: String,
    /// Proxy port.
    pub port: u16,
    /// Proxy authentication credentials.
    pub auth: Option<ProxyAuth>,
}

impl ProxyConfig {
    /// Parse a proxy configuration from a proxy URL.
    pub(crate) fn parse(value: &str) -> Result<Self, WebSocketError> {
        parse_proxy_config(value)
    }

    /// Render the proxy authority as `host:port`.
    pub(crate) fn authority(&self) -> String {
        format!("{}:{}", self.host, self.port)
    }
}

/// Resolve proxy configuration for a WebSocket URI using environment variables.
///
/// Honors `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and `NO_PROXY` (case-insensitive),
/// matching the forked tungstenite dialer.
pub(crate) fn proxy_config_from_env(uri: &Uri) -> Result<Option<ProxyConfig>, WebSocketError> {
    let tls = match uri.scheme_str() {
        Some("ws") => false,
        Some("wss") => true,
        _ => return Err(WebSocketError::Url(UrlError::UnsupportedUrlScheme)),
    };
    let host = uri
        .host()
        .ok_or(WebSocketError::Url(UrlError::NoHostName))?;
    let port = uri.port_u16().unwrap_or(if tls { 443 } else { 80 });

    if no_proxy_env_bypasses(host, port) {
        return Ok(None);
    }

    let proxy = match tls {
        false => get_env_first(&["HTTP_PROXY", "http_proxy"]),
        true => get_env_first(&["HTTPS_PROXY", "https_proxy"])
            .or_else(|| get_env_first(&["HTTP_PROXY", "http_proxy"])),
    }
    .or_else(|| get_env_first(&["ALL_PROXY", "all_proxy"]));

    let Some(proxy) = proxy else {
        return Ok(None);
    };

    parse_proxy_config(&proxy).map(Some)
}

fn get_env_first(keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|key| env::var(key).ok())
        .filter(|value| !value.is_empty())
}

fn no_proxy_env_bypasses(host: &str, port: u16) -> bool {
    let Some(no_proxy) = get_env_first(&["NO_PROXY", "no_proxy"]) else {
        return false;
    };
    no_proxy_bypasses(host, port, &no_proxy)
}

/// Whether a `NO_PROXY` value bypasses the given target.
pub(crate) fn no_proxy_bypasses(host: &str, port: u16, no_proxy: &str) -> bool {
    let host = normalize_host(host);
    let no_proxy = no_proxy.trim();
    if no_proxy.is_empty() {
        return false;
    }
    if no_proxy == "*" {
        return true;
    }

    for token in no_proxy.split(',') {
        let token = token.trim();
        if token.is_empty() {
            continue;
        }

        let (token_host, token_port) = split_host_port(token);
        if let Some(token_port) = token_port
            && token_port != port
        {
            continue;
        }

        let token_host = normalize_host(token_host);

        if host == token_host {
            return true;
        }

        if let Some(suffix) = token_host.strip_prefix('.') {
            if host == suffix || host.ends_with(&format!(".{suffix}")) {
                return true;
            }
        } else if host.ends_with(&format!(".{token_host}")) {
            return true;
        }
    }

    false
}

fn split_host_port(token: &str) -> (&str, Option<u16>) {
    let token = token.trim();
    if let Some(remainder) = token.strip_prefix('[') {
        if let Some(close) = remainder.find(']') {
            let close = close + 1;
            let host = &token[..=close];
            let remainder = &token[close + 1..];
            if let Some(port) = remainder.strip_prefix(':') {
                return (host, port.parse().ok());
            }
            return (host, None);
        }
        return (token, None);
    }

    if token.matches(':').count() == 1
        && let Some((host, port)) = token.rsplit_once(':')
    {
        return (host, port.parse().ok());
    }

    (token, None)
}

fn normalize_host(host: &str) -> &str {
    host.strip_prefix('[')
        .and_then(|h| h.strip_suffix(']'))
        .unwrap_or(host)
}

fn parse_proxy_config(value: &str) -> Result<ProxyConfig, WebSocketError> {
    let value = value.trim();
    let uri: Uri = value.parse().map_err(|_| invalid_proxy_config(value))?;

    let scheme = match uri.scheme_str() {
        Some("http") => ProxyScheme::Http,
        Some("socks5") => ProxyScheme::Socks5,
        Some("socks5h") => ProxyScheme::Socks5h,
        Some(_) | None => return Err(unsupported_proxy_scheme()),
    };

    let authority = uri
        .authority()
        .ok_or_else(|| invalid_proxy_config(value))?
        .as_str();

    let (userinfo, hostport) = split_userinfo(authority);
    let (host, port) = parse_host_port(hostport, scheme)?;

    let auth = userinfo.map(parse_userinfo).transpose()?;

    Ok(ProxyConfig {
        scheme,
        host,
        port,
        auth,
    })
}

fn split_userinfo(authority: &str) -> (Option<&str>, &str) {
    let mut iter = authority.rsplitn(2, '@');
    let hostport = iter.next().unwrap_or(authority);
    let userinfo = iter.next();
    (userinfo, hostport)
}

fn parse_host_port(hostport: &str, scheme: ProxyScheme) -> Result<(String, u16), WebSocketError> {
    let uri: Uri = format!("http://{hostport}")
        .parse()
        .map_err(|_| invalid_proxy_config(hostport))?;

    let host = uri
        .host()
        .ok_or_else(|| invalid_proxy_config(hostport))?
        .to_string();

    let port = uri.port_u16().unwrap_or(match scheme {
        ProxyScheme::Http => 80,
        ProxyScheme::Socks5 | ProxyScheme::Socks5h => 1080,
    });

    Ok((host, port))
}

fn parse_userinfo(userinfo: &str) -> Result<ProxyAuth, WebSocketError> {
    let (user, pass) = userinfo.split_once(':').unwrap_or((userinfo, ""));
    let username = percent_decode(user)?;
    let password = percent_decode(pass)?;
    Ok(ProxyAuth { username, password })
}

fn percent_decode(value: &str) -> Result<String, WebSocketError> {
    let mut output = Vec::with_capacity(value.len());
    let mut chars = value.as_bytes().iter().copied();
    while let Some(byte) = chars.next() {
        if byte == b'%' {
            let hi = chars.next().ok_or_else(|| invalid_proxy_config(value))?;
            let lo = chars.next().ok_or_else(|| invalid_proxy_config(value))?;
            let decoded = (from_hex(hi)? << 4) | from_hex(lo)?;
            output.push(decoded);
        } else {
            output.push(byte);
        }
    }
    String::from_utf8(output).map_err(|_| invalid_proxy_config(value))
}

fn from_hex(byte: u8) -> Result<u8, WebSocketError> {
    match byte {
        b'0'..=b'9' => Ok(byte - b'0'),
        b'a'..=b'f' => Ok(byte - b'a' + 10),
        b'A'..=b'F' => Ok(byte - b'A' + 10),
        _ => Err(invalid_proxy_config("invalid percent-encoding")),
    }
}

/// Establish a proxy tunnel over an async stream before the WebSocket handshake.
pub(crate) async fn connect_via_proxy<S>(
    mut stream: S,
    proxy: &ProxyConfig,
    host: &str,
    port: u16,
) -> Result<S, WebSocketError>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    match proxy.scheme {
        ProxyScheme::Http => {
            http_connect(&mut stream, host, port, proxy.auth.as_ref()).await?;
        }
        ProxyScheme::Socks5 | ProxyScheme::Socks5h => {
            socks5_handshake(&mut stream, host, port, proxy.auth.as_ref()).await?;
        }
    }

    Ok(stream)
}

fn basic_auth_header(auth: &ProxyAuth) -> String {
    let token = format!("{}:{}", auth.username, auth.password);
    format!("Basic {}", BASE64.encode(token.as_bytes()))
}

async fn read_connect_response<S>(stream: &mut S) -> Result<Vec<u8>, WebSocketError>
where
    S: AsyncRead + Unpin,
{
    let mut buf = Vec::new();
    let mut chunk = [0u8; 512];
    loop {
        if buf.len() >= MAX_CONNECT_RESPONSE_SIZE {
            return Err(proxy_connect_failed("HTTP CONNECT response too large"));
        }

        let read = stream.read(&mut chunk).await.map_err(WebSocketError::Io)?;
        if read == 0 {
            break;
        }
        buf.extend_from_slice(&chunk[..read]);
        if buf.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }

    Ok(buf)
}

/// Build the bytes for an HTTP CONNECT request.
fn build_http_connect_request(authority: &str, auth: Option<&ProxyAuth>) -> Vec<u8> {
    let mut request = Vec::new();
    request.extend_from_slice(format!("CONNECT {authority} HTTP/1.1\r\n").as_bytes());
    request.extend_from_slice(format!("Host: {authority}\r\n").as_bytes());
    request.extend_from_slice(b"Proxy-Connection: Keep-Alive\r\n");
    if let Some(auth) = auth {
        let token = basic_auth_header(auth);
        request.extend_from_slice(format!("Proxy-Authorization: {token}\r\n").as_bytes());
    }
    request.extend_from_slice(b"\r\n");
    request
}

/// Parse an HTTP CONNECT response and return the status code.
fn parse_http_connect_response(response: &[u8]) -> Result<u16, WebSocketError> {
    let text = std::str::from_utf8(response)
        .map_err(|_| proxy_connect_failed("HTTP CONNECT response not valid UTF-8"))?;

    let mut lines = text.lines();
    let status_line = lines
        .next()
        .ok_or_else(|| proxy_connect_failed("HTTP CONNECT response missing status line"))?;

    let mut parts = status_line.split_whitespace();
    let _version = parts.next();
    let code = parts
        .next()
        .ok_or_else(|| proxy_connect_failed("HTTP CONNECT response missing status code"))?;
    code.parse::<u16>()
        .map_err(|_| proxy_connect_failed("HTTP CONNECT response invalid status code"))
}

async fn http_connect<S>(
    stream: &mut S,
    host: &str,
    port: u16,
    auth: Option<&ProxyAuth>,
) -> Result<(), WebSocketError>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let authority = format!("{host}:{port}");
    let request = build_http_connect_request(&authority, auth);
    stream
        .write_all(&request)
        .await
        .map_err(WebSocketError::Io)?;
    stream.flush().await.map_err(WebSocketError::Io)?;

    let response = read_connect_response(stream).await?;
    let status = parse_http_connect_response(&response)?;
    if !(200..300).contains(&status) {
        return Err(proxy_connect_failed(format!(
            "HTTP CONNECT failed with status {status}"
        )));
    }
    Ok(())
}

async fn socks5_handshake<S>(
    stream: &mut S,
    host: &str,
    port: u16,
    auth: Option<&ProxyAuth>,
) -> Result<(), WebSocketError>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let mut methods = vec![0x00];
    if auth.is_some() {
        methods.push(0x02);
    }

    stream
        .write_all(&[0x05, methods.len() as u8])
        .await
        .map_err(WebSocketError::Io)?;
    stream
        .write_all(&methods)
        .await
        .map_err(WebSocketError::Io)?;
    stream.flush().await.map_err(WebSocketError::Io)?;

    let mut choice = [0u8; 2];
    stream
        .read_exact(&mut choice)
        .await
        .map_err(WebSocketError::Io)?;
    if choice[0] != 0x05 {
        return Err(proxy_connect_failed("SOCKS5: invalid response version"));
    }

    match choice[1] {
        0x00 => {}
        0x02 => {
            let auth = auth.ok_or_else(|| {
                proxy_connect_failed("SOCKS5: proxy requested auth, but none provided")
            })?;
            socks5_userpass_auth(stream, auth).await?;
        }
        0xFF => {
            return Err(proxy_connect_failed(
                "SOCKS5: no acceptable authentication method",
            ));
        }
        _ => {
            return Err(proxy_connect_failed(
                "SOCKS5: unsupported authentication method",
            ));
        }
    }

    send_socks5_connect(stream, host, port).await?;
    Ok(())
}

async fn socks5_userpass_auth<S>(stream: &mut S, auth: &ProxyAuth) -> Result<(), WebSocketError>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let username = auth.username.as_bytes();
    let password = auth.password.as_bytes();

    if username.len() > u8::MAX as usize || password.len() > u8::MAX as usize {
        return Err(proxy_connect_failed("SOCKS5 auth credentials too long"));
    }

    let mut buf = Vec::with_capacity(3 + username.len() + password.len());
    buf.push(0x01);
    buf.push(username.len() as u8);
    buf.extend_from_slice(username);
    buf.push(password.len() as u8);
    buf.extend_from_slice(password);

    stream.write_all(&buf).await.map_err(WebSocketError::Io)?;
    stream.flush().await.map_err(WebSocketError::Io)?;

    let mut response = [0u8; 2];
    stream
        .read_exact(&mut response)
        .await
        .map_err(WebSocketError::Io)?;
    if response[0] != 0x01 || response[1] != 0x00 {
        return Err(proxy_connect_failed("SOCKS5 authentication failed"));
    }

    Ok(())
}

async fn send_socks5_connect<S>(stream: &mut S, host: &str, port: u16) -> Result<(), WebSocketError>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let mut request = Vec::new();
    request.push(0x05);
    request.push(0x01);
    request.push(0x00);

    if let Ok(addr) = host.parse::<std::net::Ipv4Addr>() {
        request.push(0x01);
        request.extend_from_slice(&addr.octets());
    } else if let Ok(addr) = host.parse::<std::net::Ipv6Addr>() {
        request.push(0x04);
        request.extend_from_slice(&addr.octets());
    } else {
        let host_bytes = host.as_bytes();
        if host_bytes.len() > u8::MAX as usize {
            return Err(proxy_connect_failed("SOCKS5 domain name too long"));
        }
        request.push(0x03);
        request.push(host_bytes.len() as u8);
        request.extend_from_slice(host_bytes);
    }

    request.extend_from_slice(&port.to_be_bytes());
    stream
        .write_all(&request)
        .await
        .map_err(WebSocketError::Io)?;
    stream.flush().await.map_err(WebSocketError::Io)?;

    let mut header = [0u8; 4];
    stream
        .read_exact(&mut header)
        .await
        .map_err(WebSocketError::Io)?;
    if header[0] != 0x05 {
        return Err(proxy_connect_failed("SOCKS5: invalid response version"));
    }

    if header[1] != 0x00 {
        return Err(proxy_connect_failed(format!(
            "SOCKS5: connection failed with code {}",
            header[1]
        )));
    }

    let addr_len = match header[3] {
        0x01 => 4,
        0x03 => {
            let mut len = [0u8; 1];
            stream
                .read_exact(&mut len)
                .await
                .map_err(WebSocketError::Io)?;
            len[0] as usize
        }
        0x04 => 16,
        _ => return Err(proxy_connect_failed("SOCKS5: invalid address type")),
    };

    let mut discard = vec![0u8; addr_len + 2];
    stream
        .read_exact(&mut discard)
        .await
        .map_err(WebSocketError::Io)?;
    Ok(())
}

pub(crate) fn unsupported_proxy_scheme() -> WebSocketError {
    WebSocketError::Url(UrlError::UnsupportedUrlScheme)
}

pub(crate) fn invalid_proxy_config(detail: impl fmt::Display) -> WebSocketError {
    WebSocketError::Io(io::Error::new(
        io::ErrorKind::InvalidInput,
        format!("invalid proxy configuration: {detail}"),
    ))
}

fn proxy_connect_failed(message: impl fmt::Display) -> WebSocketError {
    WebSocketError::Io(io::Error::other(format!(
        "proxy connection failed: {message}"
    )))
}
