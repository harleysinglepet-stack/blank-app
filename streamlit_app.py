# app.py — DASHBOARD SINGLE PET — TV Vendas (Volume) por Marketplace — SUPABASE
# Dashboard profissional com cores e visualizações aprimoradas
# AJUSTE: usa data_importacao (operacional) em vez de data_pedido
# Vercel URL (produção): https://singlepet-dashboard.vercel.app

from __future__ import annotations

import time
from datetime import date, timedelta, datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

HTTP_TIMEOUT = 30
PAGE_SIZE = 2000
TABLE = "pedidos_tiny"

# VERCEL (apenas informativo no header/rodapé)
VERCEL_URL = "https://singlepet-dashboard.vercel.app"

# Cores profissionais por marketplace
MP_COLORS = {
    "Mercado Livre": {
        "primary": "#FFE600",
        "secondary": "#FFD700",
        "gradient": "linear-gradient(135deg, #FFE600 0%, #FFA500 100%)",
    },
    "Shopee": {
        "primary": "#EE4D2D",
        "secondary": "#FF6B4A",
        "gradient": "linear-gradient(135deg, #EE4D2D 0%, #FF8C69 100%)",
    },
    "Outros": {
        "primary": "#6B7280",
        "secondary": "#9CA3AF",
        "gradient": "linear-gradient(135deg, #6B7280 0%, #9CA3AF 100%)",
    },
}

MPS = ["Mercado Livre", "Shopee", "Outros"]


# ---------- secrets ----------
def must_get_secret(path: Tuple[str, ...]) -> str:
    cur: Any = st.secrets
    for k in path:
        cur = cur.get(k, None)
        if cur is None:
            st.error(f"❌ Faltou configurar secrets.toml: {'.'.join(path)}")
            st.stop()
    s = str(cur).strip()
    if not s:
        st.error(f"❌ secrets.toml vazio: {'.'.join(path)}")
        st.stop()
    return s


SUPABASE_URL = must_get_secret(("supabase", "url"))
SUPABASE_KEY = must_get_secret(("supabase", "anon_key"))


# ---------- helpers ----------
def iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def safe_dt(x: Any) -> Optional[datetime]:
    """
    data_importacao é timestamp without time zone.
    O Supabase REST costuma retornar string ISO.
    Parse robusto sem dependências externas.
    """
    if x is None:
        return None
    s = str(x).strip()
    if not s:
        return None
    s2 = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s2.replace("T", " "))
    except Exception:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None


def normalize_marketplace(x: Any) -> str:
    s = (str(x) if x is not None else "").strip().lower()
    if not s:
        return "Outros"
    if "shopee" in s:
        return "Shopee"
    if "mercado" in s or s == "ml" or " ml" in s or "ml " in s or "ml" == s:
        return "Mercado Livre"
    return "Outros"


def supabase_headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    }


@st.cache_data(ttl=30, show_spinner=False)
def fetch_resumo(d1: date, d2: date) -> pd.DataFrame:
    """
    Filtra por data_importacao (timestamp) e calcula o dia operacional com data_importacao::date.
    """
    select_cols = "data_importacao,marketplace"
    base = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{TABLE}"

    dt1 = f"{iso(d1)} 00:00:00"
    dt2 = f"{iso(d2)} 23:59:59"

    url = (
        f"{base}"
        f"?select={select_cols}"
        f"&codigo_produto=eq.__PEDIDO__"
        f"&data_importacao=gte.{dt1}"
        f"&data_importacao=lte.{dt2}"
    )

    rows: List[Dict[str, Any]] = []
    offset = 0

    while True:
        h = supabase_headers()
        h["Range-Unit"] = "items"
        h["Range"] = f"{offset}-{offset + PAGE_SIZE - 1}"

        r = requests.get(url, headers=h, timeout=HTTP_TIMEOUT)
        if r.status_code not in (200, 206):
            raise RuntimeError(f"Supabase HTTP {r.status_code}: {r.text[:500]}")

        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break

        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break

        offset += PAGE_SIZE
        time.sleep(0.03)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["dt"] = df["data_importacao"].apply(safe_dt)
    df = df[df["dt"].notna()].copy()
    df["d"] = pd.to_datetime(df["dt"]).dt.date

    df["marketplace"] = df["marketplace"].apply(normalize_marketplace)
    df.loc[~df["marketplace"].isin(MPS), "marketplace"] = "Outros"

    return df


def last_data_disponivel(df: pd.DataFrame) -> Optional[date]:
    if df.empty or "d" not in df.columns:
        return None
    s = df["d"].dropna()
    return None if s.empty else s.max()


def last_import_ts(df: pd.DataFrame) -> Optional[datetime]:
    if df.empty or "dt" not in df.columns:
        return None
    s = df["dt"].dropna()
    return None if s.empty else s.max()


def count_range(df: pd.DataFrame, d1: date, d2: date, mp: Optional[str] = None) -> int:
    if df.empty:
        return 0
    x = df
    if mp:
        x = x[x["marketplace"] == mp]
    m = (x["d"] >= d1) & (x["d"] <= d2)
    return int(m.sum())


def fmt_delta(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


def pct_change(current: int, previous: int) -> float:
    if previous == 0:
        return 0.0 if current == 0 else 100.0
    return ((current - previous) / previous) * 100


def daily_pivot(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    idx = pd.date_range(start=start, end=end, freq="D")

    if df.empty:
        return pd.DataFrame(index=idx, columns=MPS).fillna(0)

    g = df.groupby(["d", "marketplace"]).size().reset_index(name="qtd")
    pv = g.pivot(index="d", columns="marketplace", values="qtd").fillna(0)

    for mp in MPS:
        if mp not in pv.columns:
            pv[mp] = 0

    pv = pv[MPS].sort_index()
    pv.index = pd.to_datetime(pv.index)
    pv = pv.reindex(idx, fill_value=0)
    return pv


# ---------- Visualizações Plotly ----------
def create_comparison_chart(
    hoje_val: int, ontem_val: int, d7_val: int, prev7_val: int, mp: str, color: str
) -> go.Figure:
    fig = go.Figure()

    fig.add_trace(
        go.Bar(
            x=["Hoje", "Ontem"],
            y=[hoje_val, ontem_val],
            marker_color=color,
            text=[hoje_val, ontem_val],
            textposition="outside",
            textfont=dict(size=16, color="white", family="Arial Black"),
        )
    )

    fig.add_trace(
        go.Bar(
            x=["Últimos 7D", "7D Anteriores"],
            y=[d7_val, prev7_val],
            marker_color=MP_COLORS[mp]["secondary"],
            text=[d7_val, prev7_val],
            textposition="outside",
            textfont=dict(size=16, color="white", family="Arial Black"),
        )
    )

    fig.update_layout(
        height=200,
        margin=dict(l=0, r=0, t=10, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        xaxis=dict(
            showgrid=False,
            color="rgba(255,255,255,0.8)",
            tickfont=dict(size=12, family="Arial", color="rgba(255,255,255,0.9)"),
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor="rgba(255,255,255,0.1)",
            color="rgba(255,255,255,0.8)",
            tickfont=dict(size=11, color="rgba(255,255,255,0.7)"),
        ),
    )

    return fig


def create_trend_chart(pv: pd.DataFrame) -> go.Figure:
    fig = go.Figure()

    for mp in MPS:
        fig.add_trace(
            go.Bar(
                x=pv.index,
                y=pv[mp],
                name=mp,
                marker_color=MP_COLORS[mp]["primary"],
                hovertemplate="<b>%{x|%d/%m}</b><br>" + mp + ": %{y}<extra></extra>",
            )
        )

    fig.update_layout(
        barmode="stack",
        height=320,
        margin=dict(l=20, r=20, t=30, b=20),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Arial", color="white"),
        xaxis=dict(
            showgrid=False,
            tickformat="%d/%m",
            color="rgba(255,255,255,0.9)",
            tickfont=dict(size=11),
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor="rgba(255,255,255,0.15)",
            color="rgba(255,255,255,0.9)",
            tickfont=dict(size=12),
            title=dict(text="Pedidos", font=dict(size=13)),
        ),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="center",
            x=0.5,
            font=dict(size=12),
            bgcolor="rgba(0,0,0,0.3)",
        ),
        hovermode="x unified",
    )

    return fig


def create_donut_chart(ml: int, shp: int, out: int) -> go.Figure:
    total = ml + shp + out
    if total == 0:
        return go.Figure()

    fig = go.Figure(
        data=[
            go.Pie(
                labels=["Mercado Livre", "Shopee", "Outros"],
                values=[ml, shp, out],
                hole=0.6,
                marker=dict(
                    colors=[MP_COLORS[mp]["primary"] for mp in MPS],
                    line=dict(color="rgba(0,0,0,0.5)", width=2),
                ),
                textinfo="label+percent",
                textfont=dict(size=13, color="white", family="Arial Black"),
                hovertemplate="<b>%{label}</b><br>%{value} pedidos<br>%{percent}<extra></extra>",
            )
        ]
    )

    fig.add_annotation(
        text=f"<b>{total:,}</b><br><span style='font-size:12px'>TOTAL</span>",
        x=0.5,
        y=0.5,
        font=dict(size=28, color="white", family="Arial Black"),
        showarrow=False,
    )

    fig.update_layout(
        height=300,
        margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
    )

    return fig


# ---------- UI ----------
st.set_page_config(page_title="Dashboard Single Pet", page_icon="🐾", layout="wide")

st.markdown(
    """
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800;900&display=swap');
      * { font-family: 'Inter', -apple-system, sans-serif; }

      .block-container {
        padding-top: 28px; /* MAIS ESPAÇO PRA NÃO CORTAR A LOGO */
        padding-bottom: 1rem;
        max-width: 100%;
      }

      .hr {
        border: none;
        height: 2px;
        background: linear-gradient(90deg,
          rgba(255,230,0,0.3) 0%,
          rgba(238,77,45,0.3) 50%,
          rgba(107,114,128,0.3) 100%);
        margin: 1.0rem 0;
      }

      .subtitle {
        opacity: 0.75;
        margin-top: 6px;
        font-size: 13px;
        font-weight: 600;
        letter-spacing: 0.3px;
      }

      /* ===== BRAND HEADER (SINGLE PET) ===== */
      .brand-wrap{
        margin-top: 8px;
        margin-bottom: 10px;
      }

      .brand{
        font-size: 64px;
        font-weight: 900;
        line-height: 1;
        letter-spacing: -1.6px;
        margin: 0;
        display: inline-flex;
        align-items: baseline;
        gap: 10px;
      }

      .brand-single{
        color: #ffffff;
        padding: 10px 16px;
        border-radius: 18px;
        background: rgba(255,255,255,0.06);
        border: 1px solid rgba(255,255,255,0.10);
        box-shadow: 0 10px 28px rgba(0,0,0,0.28);
        backdrop-filter: blur(6px);
        -webkit-backdrop-filter: blur(6px);
      }

      .brand-pet{
        color: #FFE600;
        text-shadow: 0 8px 28px rgba(255,230,0,0.22);
      }

      .brand-sub{
        font-size: 14px;
        font-weight: 800;
        letter-spacing: 0.6px;
        opacity: 0.90;
        margin-top: 8px;
      }

      .brand-url{
        margin-top: 4px;
        font-size: 12px;
        opacity: 0.75;
        font-weight: 600;
      }

      .mpCard {
        background: linear-gradient(145deg, rgba(30,30,35,0.95) 0%, rgba(20,20,25,0.95) 100%);
        border: 2px solid rgba(255,255,255,0.08);
        border-radius: 20px;
        padding: 20px 18px;
        box-shadow: 0 8px 32px rgba(0,0,0,0.4);
        transition: all 0.3s ease;
        position: relative;
        overflow: hidden;
      }

      .mpCard::before {
        content: '';
        position: absolute;
        top: 0; left: 0; right: 0;
        height: 4px;
        opacity: 0.85;
      }

      .mpCard.ml::before { background: var(--ml-gradient); }
      .mpCard.shp::before { background: var(--shp-gradient); }
      .mpCard.out::before { background: var(--out-gradient); }

      :root {
        --ml-gradient: linear-gradient(135deg, #FFE600 0%, #FFA500 100%);
        --shp-gradient: linear-gradient(135deg, #EE4D2D 0%, #FF8C69 100%);
        --out-gradient: linear-gradient(135deg, #6B7280 0%, #9CA3AF 100%);
      }

      .mpTitle {
        font-size: 22px;
        font-weight: 900;
        margin: 0 0 4px 0;
        letter-spacing: -0.5px;
      }

      .mpSub {
        opacity: 0.65;
        font-size: 11px;
        margin-bottom: 14px;
        line-height: 1.4;
        font-weight: 600;
      }

      .kpi-container {
        background: rgba(255,255,255,0.03);
        border-radius: 12px;
        padding: 14px 12px;
        margin-bottom: 12px;
        border: 1px solid rgba(255,255,255,0.06);
      }

      .kpi-label {
        font-size: 11px;
        font-weight: 800;
        opacity: 0.7;
        text-transform: uppercase;
        letter-spacing: 1px;
        margin-bottom: 6px;
      }

      .kpi-value {
        font-size: 36px;
        font-weight: 900;
        line-height: 1;
        margin-bottom: 4px;
      }

      .kpi-delta {
        font-size: 13px;
        font-weight: 800;
        padding: 3px 8px;
        border-radius: 6px;
        display: inline-block;
        margin-top: 4px;
      }

      .kpi-delta.positive { background: rgba(34, 197, 94, 0.2); color: #4ade80; }
      .kpi-delta.negative { background: rgba(239, 68, 68, 0.2); color: #f87171; }
      .kpi-delta.neutral  { background: rgba(156, 163, 175, 0.2); color: #9ca3af; }

      .section-title {
        font-size: 20px;
        font-weight: 900;
        margin-bottom: 14px;
        letter-spacing: -0.3px;
      }

      .chart-container {
        background: rgba(255,255,255,0.02);
        border-radius: 16px;
        padding: 16px;
        border: 1px solid rgba(255,255,255,0.06);
      }

      .stPlotlyChart { margin: 0 !important; }
      .stAlert { background: rgba(251, 191, 36, 0.1); border-left: 4px solid #fbbf24; border-radius: 8px; }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("⚙️ Configurações")
    auto = st.toggle("🔄 Auto atualizar (30s)", value=True)
    if st.button("🔄 Atualizar agora", type="primary"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    base_periodo = st.selectbox(
        "📅 Período base:",
        ["01/02/2026", "01/01/2026"],
        index=0,
    )
    dias_tendencia = st.slider("📊 Dias da tendência", 7, 30, 14, 1)

hoje = date.today()

if base_periodo == "01/02/2026":
    inicio_base = date(2026, 2, 1)
else:
    inicio_base = date(2026, 1, 1)

inicio_mes = date(hoje.year, hoje.month, 1)
inicio_busca = min(inicio_base, inicio_mes, hoje - timedelta(days=dias_tendencia + 14))

with st.spinner("🔄 Carregando dados do Supabase..."):
    df = fetch_resumo(inicio_busca, hoje)

ultima_dia = last_data_disponivel(df)
ultima_ts = last_import_ts(df)

# ===== HEADER (SINGLE PET) =====
st.markdown(
    f"""
    <div class="brand-wrap">
      <div class="brand">
        <span class="brand-single">Single</span>
        <span class="brand-pet">Pet</span>
      </div>
      <div class="brand-sub">DASHBOARD — TV Vendas (Volume) por Marketplace</div>
      <div class="brand-url">{VERCEL_URL}</div>
    </div>
    """,
    unsafe_allow_html=True,
)

txt_ultimo_dia = ultima_dia.strftime("%d/%m/%Y") if ultima_dia else "—"
txt_ultima_ts = ultima_ts.strftime("%d/%m/%Y %H:%M") if ultima_ts else "—"

st.markdown(
    f"<div class='subtitle'>"
    f"<b>HOJE (operacional)</b>: {hoje.strftime('%d/%m/%Y')} • "
    f"<b>Último dia (importação)</b>: {txt_ultimo_dia} • "
    f"<b>Última importação</b>: {txt_ultima_ts} • "
    f"<b>Fonte</b>: Supabase / {TABLE}"
    f"</div>",
    unsafe_allow_html=True,
)

if ultima_dia and ultima_dia < hoje:
    st.warning(
        f"⚠️ Dados de hoje ({hoje.strftime('%d/%m/%Y')}) ainda não aparecem como importados. "
        f"Último dia de importação no banco: {ultima_dia.strftime('%d/%m/%Y')}."
    )

st.markdown("<div class='hr'></div>", unsafe_allow_html=True)

# Períodos
ontem = hoje - timedelta(days=1)
d7_ini = hoje - timedelta(days=6)
prev7_fim = d7_ini - timedelta(days=1)
prev7_ini = prev7_fim - timedelta(days=6)
mtd_ini = date(hoje.year, hoje.month, 1)


def mp_metrics(mp: str) -> Dict[str, Any]:
    hoje_mp = count_range(df, hoje, hoje, mp)
    ontem_mp = count_range(df, ontem, ontem, mp)
    delta_dia = hoje_mp - ontem_mp
    pct_dia = pct_change(hoje_mp, ontem_mp)

    d7_mp = count_range(df, d7_ini, hoje, mp)
    prev7_mp = count_range(df, prev7_ini, prev7_fim, mp)
    delta_7d = d7_mp - prev7_mp
    pct_7d = pct_change(d7_mp, prev7_mp)

    mtd_mp = count_range(df, mtd_ini, hoje, mp)

    return {
        "hoje": hoje_mp,
        "ontem": ontem_mp,
        "delta_dia": delta_dia,
        "pct_dia": pct_dia,
        "d7": d7_mp,
        "prev7": prev7_mp,
        "delta_7d": delta_7d,
        "pct_7d": pct_7d,
        "mtd": mtd_mp,
    }


data_ml = mp_metrics("Mercado Livre")
data_shp = mp_metrics("Shopee")
data_out = mp_metrics("Outros")

c1, c2, c3 = st.columns(3)


def render_mp_card(col, mp: str, emoji: str, d: Dict[str, Any], class_name: str):
    with col:
        st.markdown(f"<div class='mpCard {class_name}'>", unsafe_allow_html=True)
        st.markdown(f"<div class='mpTitle'>{emoji} {mp}</div>", unsafe_allow_html=True)
        st.markdown(
            f"<div class='mpSub'>"
            f"Período: Hoje ({hoje.strftime('%d/%m')}) • "
            f"Ontem ({ontem.strftime('%d/%m')}) • "
            f"7 Dias • MTD"
            f"</div>",
            unsafe_allow_html=True,
        )

        delta_class = (
            "positive" if d["delta_dia"] > 0 else ("negative" if d["delta_dia"] < 0 else "neutral")
        )

        st.markdown(
            f"""
            <div class='kpi-container'>
                <div class='kpi-label'>Hoje (operacional)</div>
                <div class='kpi-value'>{d["hoje"]:,}</div>
                <span class='kpi-delta {delta_class}'>
                    {fmt_delta(d["delta_dia"])} ({d["pct_dia"]:+.1f}%)
                </span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        col_a, col_b = st.columns(2)

        with col_a:
            delta_7d_class = (
                "positive"
                if d["delta_7d"] > 0
                else ("negative" if d["delta_7d"] < 0 else "neutral")
            )
            st.markdown(
                f"""
                <div class='kpi-container'>
                    <div class='kpi-label'>7 Dias</div>
                    <div class='kpi-value' style='font-size: 28px;'>{d["d7"]:,}</div>
                    <span class='kpi-delta {delta_7d_class}' style='font-size: 11px;'>
                        {fmt_delta(d["delta_7d"])} ({d["pct_7d"]:+.1f}%)
                    </span>
                </div>
                """,
                unsafe_allow_html=True,
            )

        with col_b:
            st.markdown(
                f"""
                <div class='kpi-container'>
                    <div class='kpi-label'>MTD</div>
                    <div class='kpi-value' style='font-size: 28px;'>{d["mtd"]:,}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        fig = create_comparison_chart(
            d["hoje"], d["ontem"], d["d7"], d["prev7"], mp, MP_COLORS[mp]["primary"]
        )
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

        st.markdown("</div>", unsafe_allow_html=True)


render_mp_card(c1, "Mercado Livre", "🟡", data_ml, "ml")
render_mp_card(c2, "Shopee", "🟠", data_shp, "shp")
render_mp_card(c3, "Outros", "⚪", data_out, "out")

st.markdown("<div class='hr'></div>", unsafe_allow_html=True)

st.markdown("<div class='section-title'>📈 Análise de Tendências</div>", unsafe_allow_html=True)

col_trend, col_dist = st.columns([2.5, 1])

with col_trend:
    st.markdown("<div class='chart-container'>", unsafe_allow_html=True)
    st.markdown(
        f"**Evolução Diária — Últimos {dias_tendencia} dias (operacional)**",
        unsafe_allow_html=True,
    )
    tstart = max(inicio_base, hoje - timedelta(days=dias_tendencia - 1))
    pv = daily_pivot(df, tstart, hoje)
    fig_trend = create_trend_chart(pv)
    st.plotly_chart(fig_trend, width="stretch", config={"displayModeBar": False})
    st.markdown("</div>", unsafe_allow_html=True)

with col_dist:
    st.markdown("<div class='chart-container'>", unsafe_allow_html=True)
    st.markdown("**Distribuição — Últimos 7 dias (operacional)**", unsafe_allow_html=True)
    fig_donut = create_donut_chart(data_ml["d7"], data_shp["d7"], data_out["d7"])
    st.plotly_chart(fig_donut, width="stretch", config={"displayModeBar": False})
    st.markdown("</div>", unsafe_allow_html=True)

st.markdown("<div class='hr'></div>", unsafe_allow_html=True)
st.caption(
    f"🔐 Fonte: Supabase ({TABLE} • codigo_produto='__PEDIDO__') | "
    f"Dashboard atualizado automaticamente | {VERCEL_URL}"
)

if auto:
    time.sleep(30)
    st.rerun()
