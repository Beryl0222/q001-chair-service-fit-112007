# 座椅换件中的规格兼容与安全交付

门店售后组收集的客户留言、维修记录、配件说明和远程检查卡。型号、地点和维修业务均为合成内容。

资料为 UTF-8 JSON。各类原始记录按编号分组，字段约定见 [资料格式](data/资料格式.md)。资料浏览命令会原样输出记录，添加 `--list` 可只查看各类数量。

使用 Python 3.11，命令只依赖标准库。请在项目根目录运行。

```sh
python3 inspect_records.py data/records.json --list
python3 -m unittest discover -v
```
