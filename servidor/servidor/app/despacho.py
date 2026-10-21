"""Área de atuação de quem dirige e regras das chamadas.

- Área: raio de 15 km em linha reta a partir do endereço ou bairro escolhido (não conta a distância pelas ruas).
- Chamada: fica 20 s com uma pessoa motorista; se não aceitar, passa para a próxima. Cada pessoa recebe a
  mesma chamada até 3 vezes. Com só uma pessoa disponível, a chamada fica concentrada nela até chegar outra.
- Distância até o embarque: no máximo 6 km em linha reta. Se houver menos de 2 pessoas disponíveis nesse raio,
  a regra é ignorada e a chamada vai para quem estiver mais perto.
"""
from __future__ import annotations

import math

AREA_RAIO_KM = 15
OFERTA_SEG = 20
OFERTAS_MAX = 3
EMBARQUE_MAX_KM = 6
POUCOS_MOTORISTAS = 2

# bairros do mapa simulado (km); com GPS real, troque por geocodificação do endereço
BAIRROS = {
    "Centro (Sé)": (120, 120), "Santana": (121, 113), "Lapa": (112, 115), "Pinheiros": (114, 122),
    "Butantã": (111, 124), "Vila Mariana": (122, 126), "Moema": (119, 128), "Santo Amaro": (113, 133),
    "Ipiranga": (125, 126), "Mooca": (124, 121), "Tatuapé": (128, 119), "Penha": (132, 117),
    "Itaquera": (140, 119), "Guarulhos": (135, 106), "Osasco": (103, 119), "São Bernardo do Campo": (128, 139),
}


def reta(a: dict, b: dict) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def na_area(m: dict, p: dict) -> bool:
    area = m.get("area")
    return not area or reta(area, p) <= area.get("raio_km", AREA_RAIO_KM)


def pool(origem: dict, candidatos: list[dict]) -> tuple[list[dict], bool]:
    """Quem pode receber a chamada, do mais perto ao mais longe, e se a regra dos 6 km foi ignorada."""
    perto = sorted(candidatos, key=lambda m: reta(m["posicao_atual"], origem))
    ate6 = [m for m in perto if reta(m["posicao_atual"], origem) <= EMBARQUE_MAX_KM]
    return (ate6, False) if len(ate6) >= POUCOS_MOTORISTAS else (perto, bool(perto) and len(ate6) < len(perto))


def proxima(oferta: dict | None, vezes: dict, lista: list[dict]) -> tuple[dict | None, bool]:
    """Escolhe quem recebe a próxima oferta. Devolve (motorista, concentrada)."""
    if not lista:
        return None, False
    if len(lista) == 1:
        return lista[0], True                      # só uma pessoa: concentra a chamada nela
    ultima = oferta and oferta.get("motorista_id")
    livres = [m for m in lista if vezes.get(m["id"], 0) < OFERTAS_MAX]
    if not livres:
        return lista[0], True                      # todas já receberam 3 vezes: concentra na mais perto
    outras = [m for m in livres if m["id"] != ultima] or livres
    return min(outras, key=lambda m: vezes.get(m["id"], 0)), False
