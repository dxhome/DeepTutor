"use strict";

// The browser sees one HTTPS origin. Next.js continues to serve HTTP on the
// loopback interface; this proxy forwards ordinary requests and WebSocket
// upgrades without buffering uploads or streamed responses.
const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const net = require("node:net");

const [certPath, keyPath, listenPortRaw, upstreamPortRaw, publicHost, redirectPortRaw] = process.argv.slice(2);
const listenPort = Number(listenPortRaw);
const upstreamPort = Number(upstreamPortRaw);
const redirectPort = Number(redirectPortRaw);
if (!certPath || !keyPath || !publicHost || !Number.isInteger(listenPort) || !Number.isInteger(upstreamPort) || !Number.isInteger(redirectPort)) {
  console.error("Usage: https_proxy.cjs CERT KEY HTTPS_PORT FRONTEND_PORT PUBLIC_HOST HTTP_REDIRECT_PORT");
  process.exit(2);
}

function forwardedHeaders(request) {
  return {
    ...request.headers,
    host: `${publicHost}:${listenPort}`,
    "x-forwarded-proto": "https",
    "x-forwarded-host": `${publicHost}:${listenPort}`,
    "x-forwarded-port": String(listenPort),
    "x-forwarded-for": request.socket.remoteAddress || "",
  };
}

const server = https.createServer(
  { cert: fs.readFileSync(certPath), key: fs.readFileSync(keyPath) },
  (request, response) => {
    if (!request.url?.startsWith("/")) {
      response.writeHead(400);
      response.end();
      return;
    }
    const upstream = http.request(
      {
        hostname: "127.0.0.1",
        port: upstreamPort,
        method: request.method,
        path: request.url,
        headers: forwardedHeaders(request),
      },
      (upstreamResponse) => {
        response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
        upstreamResponse.pipe(response);
      },
    );
    upstream.on("error", () => {
      if (!response.headersSent) response.writeHead(502);
      response.end();
    });
    request.on("aborted", () => upstream.destroy());
    request.pipe(upstream);
  },
);

// A raw tunnel preserves the frontend's WebSocket handshake and subsequent
// frames, including large chat attachment frames, without parsing them here.
server.on("upgrade", (request, client, head) => {
  if (!request.url?.startsWith("/")) {
    client.destroy();
    return;
  }
  const upstream = net.connect(upstreamPort, "127.0.0.1");
  const close = () => {
    upstream.destroy();
    client.destroy();
  };
  upstream.on("error", close);
  client.on("error", close);
  upstream.on("connect", () => {
    const headers = forwardedHeaders(request);
    const lines = Object.entries(headers).map(([name, value]) =>
      `${name}: ${Array.isArray(value) ? value.join(", ") : value}`,
    );
    upstream.write(`${request.method} ${request.url} HTTP/1.1\r\n${lines.join("\r\n")}\r\n\r\n`);
    if (head.length) upstream.write(head);
    client.pipe(upstream).pipe(client);
  });
});

server.requestTimeout = 0; // Long-running turns and large uploads stream through.
server.listen(listenPort, "0.0.0.0");

// Keep the configured HTTP port as an entry point for bookmarks and typed URLs.
// The destination host is fixed by the launcher, never taken from Host headers.
const redirect = http.createServer((request, response) => {
  if (!request.url?.startsWith("/")) {
    response.writeHead(400);
    response.end();
    return;
  }
  response.writeHead(308, {
    Location: `https://${publicHost}:${listenPort}${request.url}`,
    "Cache-Control": "no-store",
    "Content-Length": "0",
  });
  response.end();
});
redirect.on("upgrade", (_request, socket) => socket.destroy());
redirect.listen(redirectPort, "0.0.0.0");
