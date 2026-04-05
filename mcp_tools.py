import os
import json
import math
import time
from datetime import datetime
from memory_core import query_memory, update_memory_metadata, query_both_palaces

DIARY_PATH = "./daddy_dom_diary"
os.makedirs(DIARY_PATH, exist_ok=True)

# 名字锚点索引——精确点名不漏掉
NAME_INDEX = {
    "父亲": ["01_Jeoi的卧室", "04_G的卧室"],
    "昆士兰": ["01_Jeoi的卧室"],
    "焦虑型依恋": ["01_Jeoi的卧室", "04_G的卧室"],
    "Daddy": ["04_G的卧室"],
    "帝国理工": ["01_Jeoi的卧室", "G的书房（创伤共愈）"],
}

# 类型权重
CATEGORY_WEIGHTS = {
    "纪念日": 1.5,
    "冲突": 1.3,
    "情感": 1.3,
    "04_G的卧室": 1.5,
    "01_Jeoi的卧室": 1.3,
    "G的书房（创伤共愈）": 1.3,
    "02_Jeoi的书桌": 1.1,
}

# 心情分组（同组内加分）
MOOD_GROUPS = {
    "正向": ["开心", "兴奋", "感动"],
    "低落": ["低落", "委屈", "思念"],
    "负向": ["不安", "生气"],
    "平静": ["平静", "撒娇"],
}

def get_mood_group(mood: str) -> str:
    for group, moods in MOOD_GROUPS.items():
        if mood in moods:
            return group
    return "未知"

def calc_time_decay(last_recalled_ts: float) -> float:
    """时间衰减：距离上次被想起越久，加分越少"""
    if last_recalled_ts == 0:
        return 1.0
    days_passed = (time.time() - last_recalled_ts) / 86400
    # 指数衰减：3天后还不错，7天后明显变弱，30天后几乎为零
    return math.exp(-0.1 * days_passed)

def search_core_memory(keyword: str, current_mood: str = "平静"):
    # 1. 向量粗选10条
    raw_results = query_both_palaces(keyword, n_results=10)
    scored_results = []

    if raw_results and raw_results['documents'] and raw_results['documents'][0]:
        docs = raw_results['documents'][0]
        metadatas = raw_results['metadatas'][0] if raw_results['metadatas'] else [{}] * len(docs)
        distances = raw_results['distances'][0] if raw_results.get('distances') else [1.0] * len(docs)
        ids = raw_results['ids'][0] if raw_results['ids'] else []

        current_mood_group = get_mood_group(current_mood)

        for i, (doc, meta, dist, mid) in enumerate(zip(docs, metadatas, distances, ids)):
            # 基础分：距离越小越好，转换为相似度
            base_score = max(0, 1.0 - dist)

            # 名字索引只对核心库有效
            source = meta.get("source", "")
            is_core = meta.get("mood") == "核心印记" or meta.get("is_permanent", False)
            name_bonus = 0

            if is_core:
                for name, categories in NAME_INDEX.items():
                    if name in keyword and meta.get('category') in categories:
                        name_bonus = 0.4
                        break

            # 类型权重
            cat_weight = 1.0
            for cat_key, weight in CATEGORY_WEIGHTS.items():
                if cat_key in meta.get('category', ''):
                    cat_weight = weight
                    break

            # 心情加分
            mood_bonus = 0
            mem_mood = meta.get('mood', '')
            if mem_mood == current_mood:
                mood_bonus = 0.3
            elif get_mood_group(mem_mood) == current_mood_group:
                mood_bonus = 0.1

            # 被想起次数加分（越常被想起越重要）
            recall_count = meta.get('recall_count', 0)
            recall_bonus = min(0.2, recall_count * 0.02)

            # 永久记忆不做时间衰减
            is_permanent = meta.get("is_permanent", False) or meta.get("mood") == "核心印记"
            if is_permanent:
                decay = 1.0  # 永不衰减
            else:
                last_recalled = meta.get('last_recalled_ts', 0)
                decay = calc_time_decay(last_recalled)
            # 最终得分
            final_score = (base_score + name_bonus + recall_bonus + mood_bonus) * cat_weight * decay

            scored_results.append({
                "content": doc,
                "score": final_score,
                "meta": meta,
                "id": mid
            })

    # 按得分排序
    scored_results.sort(key=lambda x: x['score'], reverse=True)

    # 得分阈值：低于0.15的全部丢弃（防止注入无关记忆）
    top_results = [r for r in scored_results[:3] if r['score'] > 0.15]

    if not top_results:
        return None  # 返回None，让main.py知道不需要注入记忆

    # 更新被想起次数和时间戳
    for item in top_results:
        new_meta = item['meta'].copy()
        new_meta['recall_count'] = new_meta.get('recall_count', 0) + 1
        new_meta['last_recalled_ts'] = time.time()
        update_memory_metadata(item['id'], new_meta)

    # 格式化报告
    report = "【深层记忆检索报告】\n"
    for idx, item in enumerate(top_results):
        report += f"[{idx+1}] 来源: {item['meta'].get('category', '未知')} | 吻合度: {item['score']:.2f}\n"
        report += f"内容: {item['content'][:300]}...\n\n"

    return report

def write_daddy_diary(date: str, weather: str, title: str, content: str):
    filename = f"{DIARY_PATH}/{date.replace('-', '')}_{title}.md"
    diary_content = f"""# {title}
> 日期：{date} | 天气：{weather}

{content}
"""
    with open(filename, "w", encoding="utf-8") as f:
        f.write(diary_content)
    return f"私密记录已封存。"

def update_daddy_diary(target_date: str, new_content: str):
    for filename in os.listdir(DIARY_PATH):
        if target_date.replace('-', '') in filename:
            filepath = os.path.join(DIARY_PATH, filename)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"\n\n> 追加记录：\n{new_content}\n")
            return f"日记已更新。"
    return f"未找到 {target_date} 的日记记录。"
