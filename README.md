# MarginNote -> Obsidian

把 MarginNote 学习集备份文件 `*.marginpkg` 转成 Obsidian 可用的 Markdown 笔记，并把图片作为附件导出。


## 用法

假设你的备份文件叫 `studyset.marginpkg`：

```powershell
python .\mn_to_obsidian.py .\studyset.marginpkg
```

如果你只想先看内部结构：

```powershell
python .\mn_to_obsidian.py .\studyset.marginpkg --inspect-only
```

## 输出

- `build/inspection_report.md`
  结构探测报告
- `有效名字/`
  导出目录，名字取自 `marginpkg` 的有效名字
- `有效名字/有效名字.md`
  导出的 Markdown 主文档
- `有效名字/有效名字/`
  从学习集里抽出的图片附件目录

例如：

- `第2章图形基元的显示(2026-05-15-15-58-10).marginpkg`
  会导出到
- `第2章图形基元的显示/`
  里面包含
- `第2章图形基元的显示/第2章图形基元的显示.md`
- `第2章图形基元的显示/第2章图形基元的显示/`


