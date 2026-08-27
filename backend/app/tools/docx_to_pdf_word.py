# -*- coding: utf-8 -*-
"""用 Word COM 把 docx 转成 pdf —— 独立子进程。

    python docx_to_pdf_word.py <输入.docx> <输出.pdf>

退出码 0 表示 pdf 已生成；非 0 时 stderr 里是原因。

**为什么必须是独立进程，而不是线程。**
Word COM 挂起不是假设：弹窗、激活提示、「文档恢复」对话框都会让调用永不返回
（DEPLOYMENT.md §0 对此有专门警告）。而 `anyio.to_thread.run_sync` 既没有超时、
也不可取消——线程杀不掉，它持有的那把全局串行锁就永久不释放，
此后每一次 PDF 导出都排在它后面，永远排不到。

更糟的是这种死法探不出来：应用没崩、端口在听、health 照常 200，
看门狗只看 200，于是永远不会重启它。一个核心功能死了，所有监控却是绿的。

放进子进程，`subprocess.run(timeout=...)` 就能真的把它杀掉——
这也让 Word 分支与本就有超时的 soffice 分支形态一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

# wdFormatPDF
WD_FORMAT_PDF = 17


def convert(docx_path: Path, pdf_path: Path) -> None:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()  # 子进程主线程 STA 初始化
    try:
        # DispatchEx：独立 Word 实例，避免干扰用户已开的 Word
        word = win32com.client.DispatchEx("Word.Application")
        try:
            word.Visible = False
            word.DisplayAlerts = 0  # wdAlertsNone：关掉一切弹窗，能少一个挂起来源是一个
            doc = word.Documents.Open(str(docx_path), ReadOnly=True)
            try:
                doc.SaveAs(str(pdf_path), FileFormat=WD_FORMAT_PDF)
            finally:
                doc.Close(False)  # 不保存改动
        finally:
            word.Quit()
    finally:
        pythoncom.CoUninitialize()


def main() -> int:
    if len(sys.argv) != 3:
        print("用法: docx_to_pdf_word.py <输入.docx> <输出.pdf>", file=sys.stderr)
        return 2
    docx_path = Path(sys.argv[1])
    pdf_path = Path(sys.argv[2])
    if not docx_path.is_file():
        print(f"输入文件不存在: {docx_path}", file=sys.stderr)
        return 2
    try:
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        convert(docx_path, pdf_path)
    except Exception as exc:  # noqa: BLE001  COM 异常种类繁杂，统一转成退出码
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if not pdf_path.is_file():
        print("转换结束但没有生成 PDF 文件", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
