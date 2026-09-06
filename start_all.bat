@echo off
cd /d %~dp0
echo [1/2] 启动网页服务 (http://127.0.0.1:8310) ...
start "" python server.py 8310
echo [2/2] 启动 AI 机器人桥接 (:8766, 需要 pdk_ai 项目) ...
start "" python ai_bridge.py --port 8766
echo 完成：网页 http://127.0.0.1:8310 （桥未就绪时自动用内置AI）
pause
