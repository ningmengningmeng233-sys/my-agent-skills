# schemas/ 受控副本来源（H3 独立包内消费）

本目录是组合权威协议 `W/schemas/` 的必要子集受控复制，供本包
`SKILL.md` pipeline-contract（`schema: schemas/storyboard.schema.json`）
与 `scripts/storyboard_reader.py`（shot-table 六列表格式）消费。
独立包不再依赖技能柜相邻目录；协议定义以组合权威为准，本包不平行修改。

| 文件 | 权威来源 | sha256 | 字节 |
|---|---|---|---|
| storyboard.schema.json | W/schemas/storyboard.schema.json | 89fd837994913599b542343f0e8dd24490fc5039ce9feed30b621f8435616621 | 9064 |
| shot-table.md | W/schemas/shot-table.md | 3f721e5508f9119afca68d8be3a65d36e203668fabf5c3819065d5fd8177fc79 | 3148 |

核对：`python -c "import hashlib;print(hashlib.sha256(open('schemas/storyboard.schema.json','rb').read()).hexdigest())"` 应与上表一致。
权威更新后须同步刷新本副本并升级消费声明，不得各包私自改协议。
