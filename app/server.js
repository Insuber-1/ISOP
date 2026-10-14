'use strict';

const http = require('node:http');
const port = Number.parseInt(process.env.PORT || '3000', 10);
const server = http.createServer((req, res) => {
  if (req.url === '/health') {
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ status: 'ok' }));
    return;
  }
  if (req.url !== '/') {
    res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('ไม่พบหน้า');
    return;
  }
  res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
  res.end(`<!doctype html>
<html lang="th"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Word Editor Service</title>
<main><h1>Word Editor Service</h1><p>บริการ Node.js พร้อมทำงาน</p><p><a href="/health">ตรวจสอบสถานะบริการ</a></p></main></html>`);
});

server.on('error', (error) => {
  console.error(`Node.js service failed to listen on port ${port}: ${error.message}`);
  process.exitCode = 1;
});
server.listen(port, '0.0.0.0', () => {
  console.log(`Word Editor Service listening on 0.0.0.0:${port}`);
});
