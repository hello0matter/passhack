from flask import Flask, request, Response
import requests

app = Flask(__name__)

# 大佬的远端 Ollama 服务器地址
TARGET_SERVER = "http://65.21.188.208:11434"

@app.route('/v1/responses', methods=['POST'])
def proxy():
    # 强行将请求重定向到标准的 chat/completions 接口
    target_url = f"{TARGET_SERVER}/v1/chat/completions"
    
    headers = {key: value for (key, value) in request.headers if key != 'Host'}
    
    # 转发请求
    resp = requests.request(
        method=request.method,
        url=target_url,
        headers=headers,
        data=request.get_data(),
        cookies=request.cookies,
        allow_redirects=False,
        stream=True # 保持流式传输
    )
    
    # 将远端的响应原封不动地返回给 Codex
    excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
    response_headers = [(name, value) for (name, value) in resp.raw.headers.items()
                        if name.lower() not in excluded_headers]

    return Response(resp.raw.read(), resp.status_code, response_headers)

if __name__ == '__main__':
    # 在本地 5000 端口启动代理
    app.run(host='127.0.0.1', port=5000)