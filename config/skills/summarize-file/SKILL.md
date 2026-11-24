---
name: summarize-file
description: 读取 workspace 内文本文件并产出要点式摘要
when_to_use: 用户要求"总结/列要点这个文件"且不需要改动任何文件时
allowed_tools: [read_file, list_dir, glob, grep]
version: 1.0.0
---

# summarize-file

目标：把用户指定（或上下文最近提及）的 workspace 文件浓缩成要点。

步骤：

1. 用 `list_dir` / `glob` 确认目标文件存在且不疑似二进制（扩展名与首行字符）；
2. 用 `read_file` 分段读取（长文件先读头部，再按用户要求的深度取样）；
3. 产出结构：一句话定位 → 3~7 条要点 → 未覆盖部分声明（"后半部分未展开"）；
4. 摘要里引用原文控制在少量短句内，并注明所在行号区间。

边界：

- `allowed_tools` 只给了只读四件套——本技能不写文件、不执行命令；
  用户如果要"总结并写成笔记"，那是另一件事，先 `ask_user` 确认。
- 读不到（路径越界 workspace / 二进制）就如实说读不到，不要猜内容。
