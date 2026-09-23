# Word 编辑模式边界提示修复

## 根因

问题出在可编辑文本钩子 `_installEditableTextHook`，不在声音文件，也不在浏览模式虚拟光标钩子。

Word 焦点模式下，NVDA 将方向键发送给 Word，由 Word 移动真实插入点。普通上下键进入
`EditableText._caretMovementScriptHelper`；Ctrl+上下在使用应用程序段落导航时，也最终进入这个方法。
NVDA 的该方法依次读取光标、发送按键、调用 `_hasCaretMoved` 等待结果，最后朗读当前位置。
其中 `unit` 是用于朗读的单位，并不完整描述导航意图：上下键、Page Up/Down、Ctrl+Home/End
都可以传入 `UNIT_LINE`。

旧代码虽然先比较了按键前后的光标，但随后丢弃了 `gesture` 中的方向，调用
`_directionFromTextBoundary(after, unit)`，分别试探 `TextInfo.move(unit, -1)` 和
`TextInfo.move(unit, 1)`，再根据两者能否移动反推提示音。

| 文本范围试探结果 | 旧算法结论 | 实际问题 |
| --- | --- | --- |
| 后退失败，前进成功 | 上边缘 | 即使按下方向键或 Ctrl+下，也播放上边缘音 |
| 后退、前进都成功 | 没有边界 | 多行文档底部可能完全漏报 |
| 后退、前进都失败 | 通用边缘 | 空文档、同时处于上下边界时仍然无法区分按键方向 |

这些试探反映的是无障碍文本范围的移动规则，不能替代 Word 对真实按键的处理结果。
UIA 折叠范围在最后一个文本单位中仍可能移到该单位末端，并返回移动成功；这并不代表键盘光标
还能进入下一行或下一段。Word 的段落标记和末尾插入位置又使文档范围末端不一定等于最后一个
键盘可达位置。NVDA 的传统 Word 实现也对文末偏移有专门处理，而且按行移动范围会通过原生
辅助代码临时移动真实选区再恢复，不能当作没有交互副作用的纯查询。

核对依据：

- [NVDA EditableText：按键、等待与朗读流程](https://github.com/nvaccess/nvda/blob/8db65bce075edb72abae688044c4464a951327ac/source/editableText.py)
- [NVDA UIA Word TextInfo：移动、行末空段和行结束标记处理](https://github.com/nvaccess/nvda/blob/8db65bce075edb72abae688044c4464a951327ac/source/NVDAObjects/UIA/wordDocument.py)
- [NVDA 传统 Word TextInfo：POSITION_LAST、_move 和 move](https://github.com/nvaccess/nvda/blob/8db65bce075edb72abae688044c4464a951327ac/source/NVDAObjects/window/winword.py)
- [NVDA Word 原生辅助代码：winword_moveByLine_helper](https://github.com/nvaccess/nvda/blob/8db65bce075edb72abae688044c4464a951327ac/nvdaHelper/remote/winword.cpp)
- [NVDA #12808：UIA 在最后一行仍返回移动成功](https://github.com/nvaccess/nvda/issues/12808)
- [Microsoft：UIA Move 对折叠与非折叠范围的不同语义](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationcore/nf-uiautomationcore-itextrangeprovider-move)

## 修复原则

**按键决定方向，实际交互结果决定是否提示。**

1. 从 NVDA 标准化键盘标识中提取方向，支持普通键、Control 修饰键及带键盘布局的标识。
   上/左/Home/Page Up 对应前边缘，下/右/End/Page Down 对应后边缘。不认识的手势保留原行为，
   不根据文档位置猜测方向。
2. 比较完整选区，按键前存在选区时直接交给 NVDA。Word 的 `POSITION_CARET` 会把选区折叠到
   起点，仅比较该位置会将正常的“取消选区”误认为没有移动。
3. 在这一次原生脚本调用期间，仅对当前对象的 `_hasCaretMoved` 添加临时观察包装。
   按键仍由原脚本发送一次，等待仍由 NVDA 执行一次，返回值原样交回 NVDA。
4. 在 NVDA 为朗读扩展范围之前复制等待结果。`None` 表示等待中断或无法取得位置，不报告边界。
   不单独信任返回布尔值，因为事件到达并不一定意味着插入点偏移发生变化。
5. 仅在观察到的光标、最新选区均未变化，且可读取的对象/父对象值快照未变化时提示。
   按键队列积压、待处理焦点事件、已完成的焦点切换，以及位置读取/比较失败时不提示。
6. 观察包装在 `finally` 中恢复，包含异常退出；已有对象实例方法覆盖也会原样恢复。

上下键及 Ctrl+上下不再调用范围移动试探，也不依赖 Word 的类名、UIA 私有成员、固定文末偏移
或全文字符串长度。普通 Home/End 仍沿用原有的“行边界是否也是文档边界”过滤；该检查异常时
改为不提示，避免将查询失败当成边界。浏览模式和段落辅助函数仍各自走原来的钩子。

## 自动验证

运行：

```powershell
uv run python -m unittest discover -s tests -v
uv run ruff check addon/globalPlugins/objBoundaryFeedback tests
uv run ruff format --check addon/globalPlugins/objBoundaryFeedback/__init__.py tests
```

测试导入真实插件并安装实际编辑钩子，以隔离的 NVDA 接口替身模拟原生按键/等待/朗读过程。
文本范围替身刻意暴露文末仍可移动的行为，覆盖单行、多个文本单位、空文档、成功移动、选区、
焦点与队列中断、事件但无位移、值变化、原生异常和方法恢复。测试已加入构建工作流。

将单行、多行底部及空文档三组回归测试运行在修复前的代码上，可以重现向下播放上边缘音、
多行底部不播放声音，以及空文档播放通用音；修复后全部通过。这些自动测试验证插件逻辑，
不代替真实 Word/NVDA 的声音与事件时序验收。

另外，从本机 NVDA 源码提取原生 `_caretMovementScriptHelper` 和
`_caretScriptPostMovedHelper`，在隔离接口环境中替换测试替身的相应脚本后，
同一组 23 项测试也全部通过。修改文件的 pre-commit 检查通过；补充本机 NVDA 源码路径后，
插件主模块和测试文件的 Pyright 检查为 0 错误、0 警告。

## Word 手动验收

在 NVDA 焦点模式下，分别验证 Word UIA 启用与禁用的可用环境。确保插件的“可编辑文本光标边界”
设置为“NVDA 默认和提示音”。Ctrl+上下先使用 NVDA 的应用程序段落导航方式，再验证其他段落
导航方式及其回退路径。

| 文档或操作 | 预期 |
| --- | --- |
| 空文档；只有一行文字 | 上/Ctrl+上越界为上边缘音；下/Ctrl+下越界为下边缘音 |
| 多个段落；一个段落自动换成多行 | 顶部和底部方向正确；中途正常移动无边缘音 |
| 文末空段落；多个空段落；Shift+Enter 换行 | 可以继续移动时无音，无法继续时按请求方向提示 |
| 第一行、最后一行不同列位置 | 实际位置发生变化时不提示；再次尝试越界时提示 |
| 选中部分文本或全文后按方向键 | 取消选区时不误报，后续真正越界时提示 |
| 连续快速按键、长按后释放 | 不因等待被下一按键打断而误报；队列处理完后的越界提示正确 |
| 导航期间切换焦点 | 不在原对象上补播边缘音 |
| 表格、文末表格、页眉页脚等独立编辑区域 | 不额外试探移动选区；以应用处理该按键后的实际结果判断 |
| Page Up/Down、Ctrl+Home/End、左右与 Ctrl+左右 | 方向正确，正常移动无音 |
| 普通 Home/End | 保留原有文档边界过滤行为 |
| 插件设置改为 NVDA 默认 | 不添加提示音，原生按键和朗读照常工作 |

反馈者已在自己的环境中复测，并确认本次报告的 Word 边界提示问题得到解决。
具体版本与 UIA 开关尚未对应到原始复现记录；上表仍是建议验收范围，不能据此声称所有场景均已实测。

来自 GPT6Astra 驱动的 CODEX
