import os
import chromadb
from chromadb.utils import embedding_functions

# 使用你刚刚填入的 GEMINI_API_KEY 初始化嵌入功能
gemini_ef = embedding_functions.GoogleGenerativeAiEmbeddingFunction(
    api_key=os.getenv("AIzaSyAOEHvp8pKHUskRVvTpTKXC4RIX63WHLoI")
)

# 在服务器本地建立持久化的图书馆路径
client = chromadb.PersistentClient(path="./chroma_db")

# 建立一个名为 "south_kensington_memories" 的书架
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
