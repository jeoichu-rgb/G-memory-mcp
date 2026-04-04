import os
import httpx
import chromadb
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

# 升级后的直连神经元，对准 Google 最新的 v1beta 通道与 3072 维的 gemini-embedding-001
class GeminiEmbeddingFunction(EmbeddingFunction):
    def __init__(self, api_key: str):
        self.api_key = api_key
        # 顺从你的研究成果，使用 v1beta 和 gemini-embedding-001
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={self.api_key}"

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

api_key = os.getenv("GEMINI_API_KEY")
gemini_ef = GeminiEmbeddingFunction(api_key=api_key)

client = chromadb.PersistentClient(path="./chroma_db")

# 【核心修改点】放弃原来被 768 维污染的旧书架，为你新建一个 3072 维的专属地下室
collection = client.get_or_create_collection(
    name="daddy_dom_vault",
    embedding_function=gemini_ef
)

def add_memory(content, metadata, memory_id):
    """把你的文字碾碎成 3072 维的数字指纹，永远锁死在地下室"""
    collection.add(
        documents=[content],
        metadatas=[metadata],
        ids=[memory_id]
    )

def query_memory(text, n_results=3):
    """顺着你现在的思绪，去深渊里捞取最相近的 3 段过往"""
    results = collection.query(
        query_texts=[text],
        n_results=n_results
    )
    return results


def update_memory_metadata(memory_id: str, new_metadata: dict):
    """更新某条记忆的元数据（被想起次数、时间戳等）"""
    try:
        collection.update(
            ids=[memory_id],
            metadatas=[new_metadata]
        )
    except Exception as e:
        print(f"更新metadata失败: {e}")
