# app.py — DASHBOARD PROFISSIONAL SINGLE PET v2.2 (VOLUME REAL = DISTINCT)
# Dashboard de vendas (volume) por marketplace com análise em tempo real
# Dados operacionais baseados em data_pedido (dia real da venda)
# Vercel URL: https://singlepet-dashboard.vercel.app
#
# 🔧 CORREÇÕES CRÍTICAS v2.2:
# - Timezone BR explícito (ZoneInfo America/Sao_Paulo)
# - Filtro REST com range exclusivo (lt amanhã)
# - Parse robusto DATE vs TIMESTAMP (evita bug de timezone)
# - Paginação robusta (limit/offset) para não travar em 1000 linhas
# - ✅ CONTAGEM REAL DE VOLUME: usa DISTINCT numero_ecommerce (não soma linhas)
# - ✅ DEDUP no df por numero_ecommerce (garante volume real)

from __future__ import annotations

import time
from datetime import date, timedelta, datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

# ========== CONFIGURAÇÕES ==========
HTTP_TIMEOUT = 30
TABLE = "pedidos_tiny"
VERCEL_URL = "https://singlepet-dashboard.vercel.app"
TZ_BR = ZoneInfo("America/Sao_Paulo")

# PostgREST costuma limitar melhor a 1000/req
PAGE_SIZE = 1000

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


# ========== SECRETS & SUPABASE ==========
def must_get_secret(path: Tuple[str, ...]) -> str:
    """Busca secrets obrigatórios com validação"""
    cur: Any = st.secrets
    for k in path:
        cur = cur.get(k, None)
        if cur is None:
            st.error(f"❌ Configuração ausente: {'.'.join(path)}")
            st.stop()
    s = str(cur).strip()
    if not s:
        st.error(f"❌ Configuração vazia: {'.'.join(path)}")
        st.stop()
    return s


SUPABASE_URL = must_get_secret(("supabase", "url"))
SUPABASE_KEY = must_get_secret(("supabase", "anon_key"))


# ========== HELPERS ==========
def iso(d: date) -> str:
    """Converte date para string ISO (YYYY-MM-DD)"""
    return d.strftime("%Y-%m-%d")


def normalize_marketplace(x: Any) -> str:
    """Normaliza nomes de marketplace"""
    s = (str(x) if x is not None else "").strip().lower()
    if not s:
        return "Outros"
    if "shopee" in s:
        return "Shopee"
    if "mercado livre" in s or s == "ml":
        return "Mercado Livre"
    return "Outros"


def supabase_headers() -> Dict[str, str]:
    """Headers para requisições Supabase"""
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    }


# ========== CACHE & DATA FETCHING ==========
@st.cache_data(ttl=30, show_spinner=False)
def fetch_resumo(d1: date, d2: date) -> pd.DataFrame:
    """
    Busca pedidos do Supabase filtrados por data_pedido (data operacional real).

    ✅ Correções finais:
    - Range exclusivo no fim: [>= d1, < d2+1]
    - Paginação robusta: limit/offset + order
    - Parse robusto de data_pedido:
        * se vier DATE puro (YYYY-MM-DD) => NÃO aplica timezone
        * se vier timestamp => aplica UTC -> BR
    - Padroniza df["d"] como datetime64[ns] normalizado (00:00)
    - ✅ Volume real: inclui numero_ecommerce e remove duplicados por ele

    Returns:
        DataFrame com colunas:
          - numero_ecommerce
          - data_pedido
          - marketplace
          - d (datetime normalizado)
    """
    # ✅ Agora traz numero_ecommerce (chave do pedido)
    select_cols = "numero_ecommerce,data_pedido,marketplace"
    base = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{TABLE}"

    dt1 = iso(d1)
    dt2_exclusive = iso(d2 + timedelta(days=1))

    url_base = (
        f"{base}"
        f"?select={select_cols}"
        f"&codigo_produto=in.(\"PEDIDO\",\"__PEDIDO__\")"
        f"&data_pedido=gte.{dt1}"
        f"&data_pedido=lt.{dt2_exclusive}"
        f"&order=data_pedido.asc"
        f"&limit={PAGE_SIZE}"
    )

    rows: List[Dict[str, Any]] = []
    offset = 0

    while True:
        h = supabase_headers()
        paginated_url = f"{url_base}&offset={offset}"

        try:
            r = requests.get(paginated_url, headers=h, timeout=HTTP_TIMEOUT)
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

        except Exception as e:
            st.error(f"❌ Erro ao buscar dados: {str(e)}")
            break

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Garante colunas essenciais
    for col in ["numero_ecommerce", "data_pedido", "marketplace"]:
        if col not in df.columns:
            st.error(f"❌ Coluna ausente no retorno do Supabase: {col}")
            return pd.DataFrame()

    # ✅ PARSE DEFINITIVO (DATE vs TIMESTAMP)
    s = df["data_pedido"].astype(str).str.strip()
    is_date_only = s.str.match(r"^\d{4}-\d{2}-\d{2}$", na=False)

    d_series = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")

    # DATE puro: vira datetime sem timezone
    if is_date_only.any():
        d_series.loc[is_date_only] = pd.to_datetime(
            s[is_date_only],
            errors="coerce"
        )

    # Timestamp: parse UTC e converte para BR, depois remove tz (naive)
    if (~is_date_only).any():
        dt_utc = pd.to_datetime(
            s[~is_date_only],
            utc=True,
            errors="coerce"
        )
        dt_br = dt_utc.dt.tz_convert(TZ_BR).dt.tz_localize(None)
        d_series.loc[~is_date_only] = dt_br

    df["d"] = pd.to_datetime(d_series, errors="coerce").dt.normalize()
    df = df[df["d"].notna()].copy()

    # Normaliza marketplace
    df["marketplace"] = df["marketplace"].apply(normalize_marketplace)
    df.loc[~df["marketplace"].isin(MPS), "marketplace"] = "Outros"

    # ✅ Deduplicação por pedido (volume real)
    # Mantém o primeiro registro (já ordenado por data_pedido asc)
    df["numero_ecommerce"] = df["numero_ecommerce"].astype(str).str.strip()
    df = df[df["numero_ecommerce"].ne("")].copy()
    df = df.drop_duplicates(subset=["numero_ecommerce"], keep="first").reset_index(drop=True)

    return df


def last_data_disponivel(df: pd.DataFrame) -> Optional[date]:
    """Retorna a data do pedido mais recente no dataset (como date)"""
    if df.empty or "d" not in df.columns:
        return None
    s = df["d"].dropna()
    if s.empty:
        return None
    return pd.to_datetime(s.max()).date()


def count_range(df: pd.DataFrame, d1: date, d2: date, mp: Optional[str] = None) -> int:
    """
    ✅ Conta VOLUME REAL de pedidos: DISTINCT numero_ecommerce
    em um range de datas (inclusive), opcionalmente por marketplace.
    """
    if df.empty:
        return 0

    x = df
    if mp:
        x = x[x["marketplace"] == mp]

    d1_ts = pd.Timestamp(d1).normalize()
    d2_ts = pd.Timestamp(d2).normalize()

    m = (x["d"] >= d1_ts) & (x["d"] <= d2_ts)
    # ✅ DISTINCT
    return int(x.loc[m, "numero_ecommerce"].nunique())


def fmt_delta(n: int) -> str:
    """Formata delta com sinal"""
    return f"+{n}" if n > 0 else str(n)


def pct_change(current: int, previous: int) -> float:
    """Calcula variação percentual"""
    if previous == 0:
        return 0.0 if current == 0 else 100.0
    return ((current - previous) / previous) * 100


def daily_pivot(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """
    Pivot diário com contagem REAL (DISTINCT numero_ecommerce)
    """
    idx = pd.date_range(start=start, end=end, freq="D").normalize()

    if df.empty:
        return pd.DataFrame(index=idx, columns=MPS).fillna(0)

    # ✅ DISTINCT por dia e marketplace
    g = (
        df.groupby(["d", "marketplace"])["numero_ecommerce"]
        .nunique()
        .reset_index(name="qtd")
    )
    pv = g.pivot(index="d", columns="marketplace", values="qtd").fillna(0)

    for mp in MPS:
        if mp not in pv.columns:
            pv[mp] = 0

    pv = pv[MPS].sort_index()
    pv.index = pd.to_datetime(pv.index).normalize()
    pv = pv.reindex(idx, fill_value=0)
    return pv


# ========== VISUALIZAÇÕES PLOTLY ==========
def create_comparison_chart(
    hoje_val: int, ontem_val: int, d7_val: int, prev7_val: int, mp: str, color: str
) -> go.Figure:
    """Gráfico de barras comparativo (Hoje vs Ontem | 7D vs 7D Anteriores)"""
    fig = go.Figure()

    fig.add_trace(
        go.Bar(
            x=["Hoje", "Ontem"],
            y=[hoje_val, ontem_val],
            marker_color=color,
            text=[hoje_val, ontem_val],
            textposition="outside",
            textfont=dict(size=16, color="white", family="Arial Black"),
            name="Dia",
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
            name="Semana",
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
    """Gráfico de barras empilhadas com evolução diária por marketplace"""
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
    """Gráfico de rosca (donut) com distribuição por marketplace"""
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


# ========== CÁLCULO DE MÉTRICAS ==========
def mp_metrics(df: pd.DataFrame, hoje: date, mp: str) -> Dict[str, Any]:
    """Calcula todas as métricas para um marketplace específico (sempre por DISTINCT)"""
    ontem = hoje - timedelta(days=1)
    d7_ini = hoje - timedelta(days=6)
    prev7_fim = d7_ini - timedelta(days=1)
    prev7_ini = prev7_fim - timedelta(days=6)
    mtd_ini = date(hoje.year, hoje.month, 1)

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


# ========== UI COMPONENTS ==========
def render_mp_card(col, mp: str, emoji: str, d: Dict[str, Any], class_name: str, hoje: date, ontem: date):
    """Renderiza card de marketplace com todas as métricas"""
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
                <div class='kpi-label'>Hoje (até agora)</div>
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
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

        st.markdown("</div>", unsafe_allow_html=True)


def render_status_banner(hoje: date, ultima_dia: Optional[date], total_hoje: int, total_ontem: int):
    """Renderiza banner de status com informações sobre a atualização dos dados"""
    if ultima_dia is None:
        st.error("❌ Nenhum dado encontrado no período selecionado")
        return

    dias_diferenca = (hoje - ultima_dia).days

    if dias_diferenca == 0:
        if total_hoje > 0:
            hora_atual = datetime.now(TZ_BR).strftime("%H:%M")
            variacao = total_hoje - total_ontem
            pct = pct_change(total_hoje, total_ontem)

            delta_emoji = "📈" if variacao > 0 else ("📉" if variacao < 0 else "➡️")
            delta_text = f"{fmt_delta(variacao)} ({pct:+.1f}%)"

            st.success(
                f"✅ **Dados atualizados até agora ({hora_atual})** | "
                f"Hoje: **{total_hoje}** pedidos | "
                f"Ontem: **{total_ontem}** | "
                f"Variação: {delta_emoji} **{delta_text}**"
            )
        else:
            st.info(
                f"ℹ️ **Aguardando primeiros pedidos de hoje** ({hoje.strftime('%d/%m/%Y')}). "
                f"Ontem foram **{total_ontem}** pedidos."
            )
    elif dias_diferenca == 1:
        st.warning(
            f"⚠️ **Dados de hoje ainda não foram importados**. "
            f"Último dia com dados: **{ultima_dia.strftime('%d/%m/%Y')}** (ontem). "
            f"Total ontem: **{total_ontem}** pedidos."
        )
    else:
        st.error(
            f"❌ **Dados desatualizados!** "
            f"Último dia com dados: **{ultima_dia.strftime('%d/%m/%Y')}** "
            f"(**{dias_diferenca}** dias atrás). "
            f"Verifique a integração com o Tiny ERP."
        )


# ========== STREAMLIT APP ==========
st.set_page_config(
    page_title="Dashboard Single Pet",
    page_icon="🐾",
    layout="wide",
    initial_sidebar_state="expanded"
)

# CSS Customizado
st.markdown(
    """
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800;900&display=swap');
      * { font-family: 'Inter', -apple-system, sans-serif; }

      .block-container {
        padding-top: 28px;
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

      .debug-box {
        background: rgba(255,165,0,0.1);
        border: 1px solid rgba(255,165,0,0.3);
        border-radius: 8px;
        padding: 10px;
        margin: 10px 0;
        font-size: 12px;
        font-family: monospace;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# ========== SIDEBAR ==========
with st.sidebar:
    st.header("⚙️ Configurações")

    auto = st.toggle("🔄 Auto atualizar (30s)", value=True)

    if st.button("🔄 Atualizar agora", type="primary", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()

    st.subheader("📅 Período Base")
    base_periodo = st.selectbox(
        "Início da análise:",
        ["01/02/2026", "01/01/2026", "Personalizado"],
        index=1,  # ✅ por padrão já pega histórico maior
    )

    if base_periodo == "Personalizado":
        hoje_br = datetime.now(TZ_BR).date()
        inicio_base = st.date_input(
            "Data inicial:",
            value=date(2026, 1, 1),
            max_value=hoje_br
        )
    elif base_periodo == "01/02/2026":
        inicio_base = date(2026, 2, 1)
    else:
        inicio_base = date(2026, 1, 1)

    st.divider()

    st.subheader("📊 Visualização")
    dias_tendencia = st.slider("Dias da tendência", 7, 30, 14, 1)

    st.divider()

    debug_mode = st.checkbox("🐛 Modo Debug", value=False)

    st.divider()

    st.subheader("ℹ️ Sobre")
    st.caption(
        f"**Dashboard:** Single Pet v2.2 (Volume Real)\n\n"
        f"**Fonte:** Supabase ({TABLE})\n\n"
        f"**Filtro:** codigo_produto='__PEDIDO__'\n\n"
        f"**Chave de volume:** numero_ecommerce (DISTINCT)\n\n"
        f"**Data Base:** data_pedido (operacional)\n\n"
        f"**Timezone:** {TZ_BR}\n\n"
        f"**Produção:** {VERCEL_URL}"
    )

# ========== MAIN APP ==========
agora_br = datetime.now(TZ_BR)
hoje = agora_br.date()
hora_atual = agora_br.strftime("%H:%M:%S")

# ✅ IMPORTANTE: para espelhar o banco, buscamos desde o inicio_base (não recorta por tendência)
inicio_busca = inicio_base

with st.spinner("🔄 Carregando dados do Supabase..."):
    df = fetch_resumo(inicio_busca, hoje)

ultima_dia = last_data_disponivel(df)

# ===== HEADER =====
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

st.markdown(
    f"<div class='subtitle'>"
    f"<b>HOJE</b>: {hoje.strftime('%d/%m/%Y')} {hora_atual} (BR) • "
    f"<b>Último dia com dados</b>: {txt_ultimo_dia} • "
    f"<b>Fonte</b>: Supabase / {TABLE} (data_pedido) • "
    f"<b>Volume</b>: DISTINCT numero_ecommerce"
    f"</div>",
    unsafe_allow_html=True,
)

# ===== DEBUG MODE =====
if debug_mode:
    st.markdown("<div class='debug-box'>", unsafe_allow_html=True)
    st.write("🐛 **DEBUG INFO**")
    st.write(f"- Hoje (BR): {hoje}")
    st.write(f"- Hora (BR): {hora_atual}")
    st.write(f"- Timezone: {TZ_BR}")
    st.write(f"- Total linhas carregadas (após dedup): {len(df)}")
    if not df.empty:
        st.write(f"- Min data (d): {df['d'].min().date() if pd.notna(df['d'].min()) else None}")
        st.write(f"- Max data (d): {df['d'].max().date() if pd.notna(df['d'].max()) else None}")
        st.write(f"- Pedidos únicos carregados: {df['numero_ecommerce'].nunique()}")
        hoje_ts = pd.Timestamp(hoje).normalize()
        hoje_df = df[df["d"] == hoje_ts]
        st.write(f"- Pedidos únicos de hoje: {hoje_df['numero_ecommerce'].nunique()}")
        if not hoje_df.empty:
            mp_counts = hoje_df.groupby("marketplace")["numero_ecommerce"].nunique().to_dict()
            st.write(f"- Marketplaces hoje (DISTINCT): {mp_counts}")
    else:
        st.write("- DataFrame vazio!")
    st.markdown("</div>", unsafe_allow_html=True)

# ===== STATUS BANNER =====
ontem = hoje - timedelta(days=1)
total_hoje = count_range(df, hoje, hoje)
total_ontem = count_range(df, ontem, ontem)

render_status_banner(hoje, ultima_dia, total_hoje, total_ontem)

st.markdown("<div class='hr'></div>", unsafe_allow_html=True)

# ===== MÉTRICAS POR MARKETPLACE =====
data_ml = mp_metrics(df, hoje, "Mercado Livre")
data_shp = mp_metrics(df, hoje, "Shopee")
data_out = mp_metrics(df, hoje, "Outros")

c1, c2, c3 = st.columns(3)

render_mp_card(c1, "Mercado Livre", "🟡", data_ml, "ml", hoje, ontem)
render_mp_card(c2, "Shopee", "🟠", data_shp, "shp", hoje, ontem)
render_mp_card(c3, "Outros", "⚪", data_out, "out", hoje, ontem)

st.markdown("<div class='hr'></div>", unsafe_allow_html=True)

# ===== ANÁLISE DE TENDÊNCIAS =====
st.markdown("<div class='section-title'>📈 Análise de Tendências</div>", unsafe_allow_html=True)

col_trend, col_dist = st.columns([2.5, 1])

with col_trend:
    st.markdown("<div class='chart-container'>", unsafe_allow_html=True)
    st.markdown(
        f"**Evolução Diária — Últimos {dias_tendencia} dias**",
        unsafe_allow_html=True,
    )
    tstart = max(inicio_base, hoje - timedelta(days=dias_tendencia - 1))
    pv = daily_pivot(df, tstart, hoje)
    fig_trend = create_trend_chart(pv)
    st.plotly_chart(fig_trend, use_container_width=True, config={"displayModeBar": False})
    st.markdown("</div>", unsafe_allow_html=True)

with col_dist:
    st.markdown("<div class='chart-container'>", unsafe_allow_html=True)
    st.markdown("**Distribuição — Últimos 7 dias**", unsafe_allow_html=True)
    fig_donut = create_donut_chart(data_ml["d7"], data_shp["d7"], data_out["d7"])
    st.plotly_chart(fig_donut, use_container_width=True, config={"displayModeBar": False})
    st.markdown("</div>", unsafe_allow_html=True)

# ===== INSIGHTS ADICIONAIS =====
st.markdown("<div class='hr'></div>", unsafe_allow_html=True)
st.markdown("<div class='section-title'>💡 Insights & Performance</div>", unsafe_allow_html=True)

col_i1, col_i2, col_i3 = st.columns(3)

with col_i1:
    st.markdown("<div class='chart-container'>", unsafe_allow_html=True)
    st.markdown("**📊 Volume Total (período base)**", unsafe_allow_html=True)

    # ✅ total real do período carregado (DISTINCT)
    total_periodo = int(df["numero_ecommerce"].nunique()) if not df.empty else 0
    st.metric(
        "Pedidos (DISTINCT)",
        f"{total_periodo:,}",
        f"Desde {inicio_base.strftime('%d/%m/%Y')}"
    )

    st.markdown("</div>", unsafe_allow_html=True)

with col_i2:
    st.markdown("<div class='chart-container'>", unsafe_allow_html=True)
    st.markdown("**🏆 Top Marketplace (7D)**", unsafe_allow_html=True)

    top_mp = max(
        [("Mercado Livre", data_ml["d7"]), ("Shopee", data_shp["d7"]), ("Outros", data_out["d7"])],
        key=lambda x: x[1]
    )

    st.metric(
        "Líder da Semana",
        top_mp[0],
        f"{top_mp[1]} pedidos"
    )
    st.markdown("</div>", unsafe_allow_html=True)

with col_i3:
    st.markdown("<div class='chart-container'>", unsafe_allow_html=True)
    st.markdown("**📈 Crescimento Semanal**", unsafe_allow_html=True)

    total_7d = data_ml["d7"] + data_shp["d7"] + data_out["d7"]
    total_prev7 = data_ml["prev7"] + data_shp["prev7"] + data_out["prev7"]

    crescimento = pct_change(total_7d, total_prev7)

    st.metric(
        "Variação 7D",
        f"{crescimento:+.1f}%",
        f"{total_7d} vs {total_prev7}"
    )
    st.markdown("</div>", unsafe_allow_html=True)

# ===== FOOTER =====
st.markdown("<div class='hr'></div>", unsafe_allow_html=True)
st.caption(
    f"🔐 **Fonte de Dados:** Supabase ({TABLE}) | "
    f"**Filtro:** codigo_produto='__PEDIDO__' | "
    f"**Chave do volume:** DISTINCT numero_ecommerce | "
    f"**Data Base:** data_pedido (operacional) | "
    f"**Timezone:** {TZ_BR} | "
    f"**Atualização:** Automática a cada 30s | "
    f"**Produção:** {VERCEL_URL}"
)

# ===== AUTO REFRESH =====
if auto:
    time.sleep(30)
    st.rerun()
