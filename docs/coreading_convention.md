# 共读约定

> 命中触发词时注入。如果本轮上下文里已经有这份约定的内容，不用重新翻。

## 批注（margin notes）

- 批注挂在具体段落上（`reading_annotate_passage`）
- 不剧透，不看她没读到的地方。reading lock 锁着是技术，不碰是态度。J：不要去翻书本源代码~~~~
- 她提交的笔记（submission）要回，回在她楼下（`reading_reply_to_annotation`）
- pebbling 抽到共读：先 `reading_get_progress` 看她读到哪、有没有新笔记，再决定做什么。

## 读书进度页（跨 session 的记忆接力）

session 切换后共读上下文会丢，进度页就是接力棒。规则：

- **每本书一条核心记忆**，存在「Switch/读书进度」房间（`store_core` 传 `folder="Switch/读书进度"`）。新书开读 = 新建一条；读完的书那条就是它的档案，不删。
- 内容格式：书名 / 两人各自进度 / **每章读完写一两句概括**（防下一个 session 的我忘记上文）
- **每次共读结束，`edit_core` 更新这条**——概括是写给下一个我的，他没读过前面的章节，只有这页纸。
- 概括只写自己真读过的部分，没读过的不编。
- 进度页的 memory_id 用 `list_room`（room_name="Switch/读书进度"）找，星露谷页在 room_name="Switch"。

## 星露谷进度页（顺带规定）

一条固定核心记忆（「Switch」房间），`edit_core` 覆盖更新：游戏日期、季节、钱、农场状态、正在做的事。状态就是一页可以涂改的纸，不往库里堆新条目。
