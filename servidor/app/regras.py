"""Regras de negócio do Motora: tarifa, regiões, limites e frota permitida.

As posições usam um plano em km (x, y), igual ao mapa do protótipo. Para usar
GPS real, troque `dist` por haversine em lat/lng e `rota_padrao` por uma API de
rotas (Google, Mapbox ou OSRM).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# ---------- tarifa ----------
BANDEIRADA = 4.00
POR_KM = 2.20
POR_MIN = 0.40
ESPERA_GRATIS_SEG = 180
ESPERA_POR_MIN = 0.60
NOTURNO = 0.20            # +20%
NOTURNO_INICIO = 22
NOTURNO_FIM = 6
TARIFA_MINIMA = 8.00
LIMITE_KM = 100
TAXA_PLATAFORMA = 0.15
VELOCIDADE_MEDIA = 40     # km/h, usada para estimar o tempo
DESVIO_ALERTA_KM = 1.0    # distância da rota que dispara alerta
MAPA_KM = 240

# ---------- frota: só elétricos e híbridos ----------
ELETRICOS = {
    "BYD Dolphin": 400, "BYD Dolphin Mini": 280, "BYD Seal": 500, "BYD Yuan Plus": 420,
    "Renault Kwid E-Tech": 250, "Renault Megane E-Tech": 450, "Chevrolet Bolt EUV": 390,
    "Chevrolet Spark EUV": 350, "GWM Ora 03": 400, "Volvo EX30": 330, "Peugeot e-2008": 320,
    "Fiat 500e": 230, "JAC E-JS1": 280, "Nissan Leaf": 270,
}
HIBRIDOS = {
    "Toyota Corolla Hybrid": "híbrido flex", "Toyota Corolla Cross Hybrid": "híbrido flex",
    "Toyota Yaris Cross Hybrid": "híbrido flex", "Honda Civic e:HEV": "híbrido",
    "BYD King": "híbrido plug-in", "BYD Song Pro": "híbrido plug-in", "BYD Song Plus": "híbrido plug-in",
    "GWM Haval H6": "híbrido plug-in", "Caoa Chery Tiggo 7 Hybrid": "híbrido", "Kia Niro Hybrid": "híbrido",
}


def veiculo_permitido(modelo: str) -> bool:
    return modelo in ELETRICOS or modelo in HIBRIDOS


# ---------- geometria ----------
Ponto = dict  # {"x": km, "y": km}


def dist(a: Ponto, b: Ponto) -> float:
    """Distância pelas ruas (grade), em km."""
    return abs(a["x"] - b["x"]) + abs(a["y"] - b["y"])


def rota_padrao(a: Ponto, b: Ponto) -> list[Ponto]:
    return [dict(a), {"x": b["x"], "y": a["y"]}, dict(b)]


def comprimento(rota: list[Ponto]) -> float:
    return sum(dist(rota[i - 1], rota[i]) for i in range(1, len(rota)))


def distancia_da_rota(p: Ponto, rota: list[Ponto]) -> float:
    """Menor distância em linha reta entre o ponto e a rota, em km."""
    melhor = math.inf
    for a, b in zip(rota, rota[1:]):
        ax, ay, bx, by = a["x"], a["y"], b["x"], b["y"]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((p["x"] - ax) * dx + (p["y"] - ay) * dy) / L))
        cx, cy = ax + t * dx, ay + t * dy
        melhor = min(melhor, math.hypot(p["x"] - cx, p["y"] - cy))
    return melhor


# ---------- regiões e dinâmica ----------
REGIOES_INICIAIS = {
    "Centro": {"chamadas": 18, "motoristas": 9},
    "Região Norte": {"chamadas": 6, "motoristas": 7},
    "Região Leste": {"chamadas": 22, "motoristas": 8},
    "Região Oeste": {"chamadas": 9, "motoristas": 5},
    "Região Sul": {"chamadas": 5, "motoristas": 6},
}


def regiao_de(p: Ponto) -> str:
    x, y = p["x"], p["y"]
    if 80 <= x <= 160 and 80 <= y <= 160:
        return "Centro"
    if y < 120:
        return "Região Norte" if x < 120 else "Região Leste"
    return "Região Oeste" if x < 120 else "Região Sul"


def fator_dinamico(chamadas: int, motoristas: int) -> float:
    r = chamadas / max(1, motoristas)
    if r <= 1:
        return 1.0
    if r <= 1.5:
        return 1.2
    if r <= 2:
        return 1.4
    if r <= 3:
        return 1.6
    return 1.8


def noturno(hora: int) -> bool:
    return hora >= NOTURNO_INICIO or hora < NOTURNO_FIM


# ---------- preço ----------
@dataclass
class Tarifa:
    corrida: float
    dinamica: float
    espera: float
    noturno: float
    adicional: float
    total: float
    minimo: bool

    def dict(self) -> dict:
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in self.__dict__.items()}


def calcular(km: float, minutos: float, espera_seg: float, eh_noturno: bool, fator: float,
             adicional: float = 0.0) -> Tarifa:
    corrida = BANDEIRADA + km * POR_KM + minutos * POR_MIN
    dinamica = corrida * (fator - 1)
    espera = max(0.0, espera_seg - ESPERA_GRATIS_SEG) / 60 * ESPERA_POR_MIN
    sub = corrida + dinamica + espera
    acres = sub * NOTURNO if eh_noturno else 0.0
    total = sub + acres
    minimo = total < TARIFA_MINIMA
    if minimo:
        total = TARIFA_MINIMA
    return Tarifa(corrida, dinamica, espera, acres, adicional, total + adicional, minimo)


def valor_rota_alternativa(km_extra: float, fator: float, eh_noturno: bool) -> float:
    minutos = km_extra / VELOCIDADE_MEDIA * 60
    v = (km_extra * POR_KM + minutos * POR_MIN) * fator * (1 + NOTURNO if eh_noturno else 1)
    return round(v, 2)
