"""Gera static/index.html a partir do protótipo (motora.html), envolvendo-o num documento HTML completo.

Uso: python gerar_index.py caminho/para/motora.html
"""
import sys
from pathlib import Path

fonte = Path(sys.argv[1]).read_text(encoding="utf-8")
cabeca, corpo = fonte.split("</style>\n", 1)
html = f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>:root{{padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}}body{{margin:0;font-size:14px}}img{{max-width:100%}}[hidden]{{display:none!important}}</style>
{cabeca}</style>
</head>
<body>
{corpo}
</body>
</html>
"""
(Path(__file__).parent / "static" / "index.html").write_text(html, encoding="utf-8")
print("static/index.html gerado")
