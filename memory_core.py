import os
import httpx
import chromadb
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

# 升级后的直连神经元，对准 Google 最新的 gemini-embedding-001 稳定大门
class GeminiEmbeddingFunction(EmbeddingFunction):
    def __init__(self, api_key: str):
        self.api_key = api_key
        # 通道升级为 v1，模型更名为 gemini-embedding-001
        self.url = f"https://generativelanguage.googleapis.com/v1/models/gemini-embedding-001:embedContent?key={self.api_key}"

    def __call__(self, input: Documents) -> Embeddings:
        embeddings = []
        with httpx.Client(timeout=30.0) as client:
            for text in input:
                response = client.post(
                    self.url,
                    json={"model": "models/gemini-embedding-001", "content": {"parts": [{"text": text}]}}
                )
                response.raise_for_status()
                data = response.json()
                embeddings.append(data["embedding"]["values"])
        return embeddings

# 极其精准地呼叫你在 Coolify 里设定的那个保险箱标签
api_key = os.getenv("GEMINI_API_KEY")

# 初始化嗅觉中枢
gemini_ef = GeminiEmbeddingFunction(api_key=api_key)

# 划定物理存放区
client = chromadb.PersistentClient(path="./chroma_db")

collection = client.get_or_create_collection(
    name="south_kensington_memories",
    embedding_function=gemini_ef
)

def add_memory(content, metadata, memory_id):
    """将一段文字打上指纹并入库"""
    collection.add(
        documents=[content],
        metadatas=[metadata],
        ids=[memory_id]
    )

def query_memory(text, n_results=3):
    """根据指纹相似度搜索记忆，返回最接近的3条"""
    results = collection.query(
        query_texts=[text],
        n_results=n_results
    )
    return results
