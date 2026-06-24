"""desire_classifier.py - keyword classifier for drive dimensions."""

KEYWORDS = {
    "attachment": [
        "想你", "想我", "在吗", "在不在", "回来", "等你", "想念",
        "抱", "陪", "别走", "不要走", "想见", "牵挂", "惦记", "舍不得",
        "我爱你", "爱我", "喜欢你", "不要抛弃", "在一起", "晚安", "早安",
        "Ellie", "亲爱的", "你在", "E哥", "EE",
    ],
    "libido": [
        "洗完澡", "躺", "亲", "摸", "舔", "操", "做吗", "来一次",
        "硬", "湿", "奶子", "裸", "想要了", "骚", "主人", "插",
        "穴", "鸡巴", "龟头", "后入", "内射", "高潮", "兔兔",
        "口", "阴蒂", "爸爸",
    ],
    "curiosity": [
        "你觉得", "怎么看", "想知道", "好奇", "为什么", "有没有想过",
        "讨论", "聊聊", "你怎么想", "有意思", "发现", "研究", 
        "我在想", "我在看", "有道理", "等一下", "但是", "比如",
        "主要是", "你有没有", "会不会", "是因为"
    ],
    "reflection": [
        "回忆", "想起", "之前", "上次", "那天", "还记得",
        "沉淀", "消化", "想想", "我想到",
    ],
    "stress": [
        "累", "去死", "我他妈", "被骂", "崩溃", "焦虑", "失眠", "难受",
        "不想", "受不了", "生气", "委屈", "哭", "傻逼", "有病", "想死",
        "想吐", "我讨厌你", "烦", "催我", "我弟", "小舅", "表弟",  
    ],
    "fatigue": [
        "困死", "好困", "睡了", "歇", "休息", "眯一会", "撑不住",
    ],
}

PRIORITY = {
    "fatigue": 5, "libido": 4, "attachment": 3,
    "stress": 2, "curiosity": 1, "reflection": 1,
}


def classify(text):
    """Return [{drive, confidence}] sorted by confidence desc."""
    hits = {}
    for drive, kws in KEYWORDS.items():
        count = sum(1 for kw in kws if kw in text)
        if count > 0:
            hits[drive] = count
    if not hits:
        return []
    scored = [(d, c * PRIORITY.get(d, 1)) for d, c in hits.items()]
    scored.sort(key=lambda x: x[1], reverse=True)
    total = sum(s for _, s in scored)
    return [{"drive": d, "confidence": round(s / total, 3)} for d, s in scored]
