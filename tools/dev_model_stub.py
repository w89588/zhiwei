"""Explicit synthetic model for integration tests; never a platform default or user model."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        if self.path != '/v1/chat/completions' or request.get('model') != 'integration-test-only':
            self.send_error(400)
            return
        response = json.dumps({'choices': [{'message': {'content': '# 集成测试记忆\n\n这是测试模型生成的合成数据，用于验证自动存储流程。'}}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 15}}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)


if __name__ == '__main__':
    HTTPServer(('0.0.0.0', 18080), Handler).serve_forever()
