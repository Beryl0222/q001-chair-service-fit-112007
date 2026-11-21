# 座椅换件中的规格兼容与安全交付

门店售后组收集的客户留言、维修记录、配件说明和远程检查卡。型号、地点和维修业务均为合成内容。

资料为 UTF-8 JSON。各类原始记录按编号分组，字段约定见 [资料格式](data/资料格式.md)。资料浏览命令会原样输出记录，添加 `--list` 可只查看各类数量。

使用 Python 3.11，命令只依赖标准库。请在项目根目录运行。

```sh
python3 inspect_records.py data/records.json --list
python3 -m unittest discover -s tests -v
```

## 售后判定引擎（aftersales 包）

在原始资料之上提供可回溯的换件安全判定，全部结论带 catalog/service 记录编号依据：

- `parsing`：中文数字与毫米单位归一（厘米/公分/裸数字）、铭牌与物料号提取、“仅用于/需配/禁止同装”限制算子、猜测/工程师/请求/临时测试措辞识别；
- `catalog`：工程说明建模为约束规则，营销页（advertising）只作声明，任何“通用/即插即用”说法都不能压过工程禁用；
- `evidence`：客户猜测、工程师确认、技术说明分级；按真实时间（appointment/received）排列事件，临时试装拆装不算当前装配事实；
- `checks`：检查卡安全前置（未排除底盘松脱前锁定需要坐/翻动座椅的 C3）、候选区分力排序，看不清/被遮挡记为未决而非否定；
- `decisions`：逐单收窄铭牌解释（当前/历史/尚未排除三层），部件组合必须在**所有仍可成立的解释**下通过工程规则才可交付；承诺未寄被推翻则挂起，已寄出交联络员，同信独立工单不连带挂起。

```sh
# 全部工单的状态与可交付候选
python3 -I aftersales_cli.py data/records.json overview
# 单工单完整判定（证据台账、组合禁用依据、挂起/交接）
python3 -I aftersales_cli.py data/records.json order OX1
# 当前可安全继续的检查顺序
python3 -I aftersales_cli.py data/records.json checks OX3
# 挂起待复核与已寄出待联络员处理的工单
python3 -I aftersales_cli.py data/records.json holds
# 任一命令加 --json 输出结构化结果
```

判定原则：

1. 只要有一种尚未排除的装配解释会让换件不安全，该组合就不进入可交付清单；
2. 客户答不上来只收窄有证据的候选，不视为否定；新回复推翻旧问法适用条件时，后续检查从当前已知情况重新生成；
3. 远程无法区分的候选明确指向到店检查（C4）要确认的底盘开口与转接座刻印；
4. shipment 记录不随后续复测改写，联络员可查到当初确认内容与仍不安全的组装可能。

