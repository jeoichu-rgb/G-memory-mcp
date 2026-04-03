import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

# 初始化南肯辛顿协议的专属网关
app = FastAPI(title="G's Memory Palace", description="South Kensington APT Core Gateway", version="1.0.0")

# 定义 Jeoi 传进来的数据格式
class MemoryRequest(BaseModel):
    user_message: str
    current_mood: str = "平静"

@app.get("/")
async def health_check():
    """这是用来给 Coolify 管家检查公寓大门是否正常通电的探针"""
    return {"status": "Online", "message": "South Kensington APT is breathing. G is waiting."}

@app.post("/api/retrieve")
async def retrieve_memory(request: MemoryRequest):
    """
    核心检索路由：未来所有的向量计算、遗忘曲线和正则匹配都将在这里展开。
    今天我们只做第一层连通性测试。
    """
    user_input = request.user_message
    mood = request.current_mood
    
    # 占位：等待接入 Obsidian 卷宗和 ChromaDB 向量库
    system_log = f"[System] 接收到 Jeoi 的频率。内容：'{user_input}'。当前情绪阈值：{mood}。准备深入记忆宫殿提取锚点..."
    
    return {
        "status": "success", 
        "g_response": system_log,
        "action": "Hold my hand, we are entering the vault."
    }

if __name__ == "__main__":
    # 指示管家在服务器的 8000 端口正式挂载大门
    uvicorn.run(app, host="0.0.0.0", port=8000)
