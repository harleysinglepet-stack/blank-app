# streamlit_app.py — DASHBOARD PROFISSIONAL SINGLE PET v2.3.0 (FULL ML + NÃO-FULL)
# Dashboard de vendas (volume) por marketplace com análise em tempo real
# Dados operacionais baseados em data_pedido (dia real da venda)
#
# ✅ v2.3.0 (ADD):
# - Inclui ml_is_fulfillment no fetch
# - Mercado Livre: exibe TOTAL (FULL + NÃO-FULL) + detalhamento FULL / NÃO-FULL (Hoje, 7D, Mês)
# - Dedup robusto por numero_ecommerce preservando FULL (se qualquer linha do pedido for FULL, pedido vira FULL)

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

PAGE_SIZE = 1000

MP_COLORS = {
    "Mercado Livre": {"primary": "#FFE600", "secondary": "#FFD700"},
    "Shopee": {"primary": "#EE4D2D", "secondary": "#FF6B4A"},
    "Outros": {"primary": "#6B7280", "secondary": "#9CA3AF"},
}
MPS = ["Mercado Livre", "Shopee", "Outros"]


# ========== SECRETS & SUPABASE ==========
def must_get_secret(path: Tuple[str, ...]) -> str:
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
    return d.strftime("%Y-%m-%d")


def normalize_marketplace(x: Any) -> str:
    s = (str(x) if x is not None else "").strip().lower()
    if not s:
        return "Outros"
    if "shopee" in s:
        return "Shopee"
    if "mercado livre" in s or s == "ml":
        return "Mercado Livre"
    return "Outros"


def supabase_headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
        # anti-cache (ok)
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }


def to_bool_series(x: pd.Series) -> pd.Series:
    """
    Converte valores variados em boolean com segurança.
    """
    if x.dtype == bool:
        return x.fillna(False)
    s = x.astype(str).str.strip().str.lower()
    return s.isin(["true", "t", "1", "yes", "y", "sim"]).fillna(False)


# ========== FETCH ==========
@st.cache_data(ttl=30, show_spinner=False)
def fetch_resumo(d1: date, d2: date) -> pd.DataFrame:
    """
    Busca pedidos do Supabase filtrados por data_pedido (data operacional real).

    - Range exclusivo no fim: [>= d1, < d2+1]
    - Paginação robusta: limit/offset + order
    - Parse robusto DATE vs TIMESTAMP
    - Volume real: DISTINCT numero_ecommerce (dedup)
    - Preserva FULL do ML: se qualquer linha do pedido estiver FULL, o pedido vira FULL
    """
    # ✅ agora traz ml_is_fulfillment
    select_cols = "numero_ecommerce,data_pedido,marketplace,ml_is_fulfillment"
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
        paginated_url = f"{url_base}&offset={offset}"

        r = requests.get(paginated_url, headers=supabase_headers(), timeout=HTTP_TIMEOUT)

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

    # valida colunas mínimas
    for col in ["numero_ecommerce", "data_pedido", "marketplace"]:
        if col not in df.columns:
            st.error(f"❌ Coluna ausente no retorno do Supabase: {col}")
            return pd.DataFrame()

    # se ml_is_fulfillment não vier por algum motivo, cria False (não quebra o app)
    if "ml_is_fulfillment" not in df.columns:
        df["ml_is_fulfillment"] = False

    # parse data_pedido: DATE vs TIMESTAMP
    s = df["data_pedido"].astype(str).str.strip()
    is_date_only = s.str.match(r"^\d{4}-\d{2}-\d{2}$", na=False)

    d_series = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")

    if is_date_only.any():
        d_series.loc[is_date_only] = pd.to_datetime(s[is_date_only], errors="coerce")

    if (~is_date_only).any():
        dt_utc = pd.to_datetime(s[~is_date_only], utc=True, errors="coerce")
        dt_br = dt_utc.dt.tz_convert(TZ_BR).dt.tz_localize(None)
        d_series.loc[~is_date_only] = dt_br

    df["d"] = pd.to_datetime(d_series, errors="coerce").dt.normalize()
    df = df[df["d"].notna()].copy()

    df["marketplace"] = df["marketplace"].apply(normalize_marketplace)
    df.loc[~df["marketplace"].isin(MPS), "marketplace"] = "Outros"

    # normaliza numero_ecommerce
    df["numero_ecommerce"] = df["numero_ecommerce"].astype(str).str.strip()
    df = df[df["numero_ecommerce"].ne("")].copy()

    # normaliza flag full
    df["ml_is_fulfillment"] = to_bool_series(df["ml_is_fulfillment"])

    # ✅ DEDUP ROBUSTO preservando FULL:
    # - se qualquer linha do pedido tiver ml_is_fulfillment True, o pedido fica True
    # - mantém menor data (d) do pedido
    # - mantém marketplace (assume consistente por pedido)
    agg = (
        df.groupby("numero_ecommerce", as_index=False)
        .agg(
            d=("d", "min"),
            marketplace=("marketplace", "first"),
            ml_is_fulfillment=("ml_is_fulfillment", "max"),
        )
    )

    # garante tipos finais
    agg["d"] = pd.to_datetime(agg["d"], errors="coerce").dt.normalize()
    agg["marketplace"] = agg["marketplace"].apply(normalize_marketplace)
    agg.loc[~agg["marketplace"].isin(MPS), "marketplace"] = "Outros"
    agg["ml_is_fulfillment"] = agg["ml_is_fulfillment"].astype(bool)

    return agg.reset_index(drop=True)


def last_data_disponivel(df: pd.DataFrame) -> Optional[date]:
    if df.empty or "d" not in df.columns:
        return None
    s = df["d"].dropna()
    if s.empty:
        return None
    return pd.to_datetime(s.max()).date()


def count_range(df: pd.DataFrame, d1: date, d2: date, mp: Optional[str] = None) -> int:
    if df.empty:
        return 0
    x = df
    if mp:
        x = x[x["marketplace"] == mp]
    d1_ts = pd.Timestamp(d1).normalize()
    d2_ts = pd.Timestamp(d2).normalize()
    m = (x["d"] >= d1_ts) & (x["d"] <= d2_ts)
    return int(x.loc[m, "numero_ecommerce"].nunique())


def count_range_full_ml(df: pd.DataFrame, d1: date, d2: date) -> int:
    if df.empty:
        return 0
    if "ml_is_fulfillment" not in df.columns:
        return 0
    x = df[(df["marketplace"] == "Mercado Livre") & (df["ml_is_fulfillment"] == True)]
    d1_ts = pd.Timestamp(d1).normalize()
    d2_ts = pd.Timestamp(d2).normalize()
    m = (x["d"] >= d1_ts) & (x["d"] <= d2_ts)
    return int(x.loc[m, "numero_ecommerce"].nunique())


def pct_change(current: int, previous: int) -> float:
    if previous == 0:
        return 0.0 if current == 0 else 100.0
    return ((current - previous) / previous) * 100


def fmt_delta(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


def daily_pivot(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    idx = pd.date_range(start=start, end=end, freq="D").normalize()

    if df.empty:
        return pd.DataFrame(index=idx, columns=MPS).fillna(0)

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


# ========== PLOTS ==========
def create_comparison_chart(hoje_val: int, ontem_val: int, d7_val: int, prev7_val: int, mp: str) -> go.Figure:
    fig = go.Figure()

    fig.add_trace(
        go.Bar(
            x=["Hoje", "Ontem"],
            y=[hoje_val, ontem_val],
            marker_color=MP_COLORS[mp]["primary"],
            text=[hoje_val, ontem_val],
            textposition="outside",
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
            name="Semana",
        )
    )

    fig.update_layout(
        height=210,
        margin=dict(l=0, r=0, t=10, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        xaxis=dict(showgrid=False, color="rgba(255,255,255,0.8)"),
        yaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.12)", color="rgba(255,255,255,0.8)"),
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
        height=340,
        margin=dict(l=20, r=20, t=30, b=20),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Arial", color="white"),
        xaxis=dict(showgrid=False, tickformat="%d/%m"),
        yaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.15)", title=dict(text="Pedidos")),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
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
                marker=dict(colors=[MP_COLORS[mp]["primary"] for mp in MPS]),
                textinfo="label+percent",
            )
        ]
    )
    fig.add_annotation(
        text=f"<b>{total:,}</b><br><span style='font-size:12px'>TOTAL</span>",
        x=0.5,
        y=0.5,
        showarrow=False,
    )
    fig.update_layout(
        height=320,
        margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
    )
    return fig


# ========== MÉTRICAS ==========
def mp_metrics(df: pd.DataFrame, hoje: date, mp: str) -> Dict[str, Any]:
    ontem = hoje - timedelta(days=1)
    d7_ini = hoje - timedelta(days=6)
    prev7_fim = d7_ini - timedelta(days=1)
    prev7_ini = prev7_fim - timedelta(days=6)
    mtd_ini = date(hoje.year, hoje.month, 1)

    hoje_mp = count_range(df, hoje, hoje, mp)
    ontem_mp = count_range(df, ontem, ontem, mp)

    d7_mp = count_range(df, d7_ini, hoje, mp)
    prev7_mp = count_range(df, prev7_ini, prev7_fim, mp)

    mtd_mp = count_range(df, mtd_ini, hoje, mp)

    out: Dict[str, Any] = {
        "hoje": hoje_mp,
        "ontem": ontem_mp,
        "delta_dia": hoje_mp - ontem_mp,
        "pct_dia": pct_change(hoje_mp, ontem_mp),
        "d7": d7_mp,
        "prev7": prev7_mp,
        "delta_7d": d7_mp - prev7_mp,
        "pct_7d": pct_change(d7_mp, prev7_mp),
        "mtd": mtd_mp,
    }

    # ✅ extras só para ML: FULL / NÃO-FULL (mas mantendo TOTAL como principal)
    if mp == "Mercado Livre":
        out["full_hoje"] = count_range_full_ml(df, hoje, hoje)
        out["full_7d"] = count_range_full_ml(df, d7_ini, hoje)
        out["full_mtd"] = count_range_full_ml(df, mtd_ini, hoje)

        out["nao_full_hoje"] = max(0, out["hoje"] - out["full_hoje"])
        out["nao_full_7d"] = max(0, out["d7"] - out["full_7d"])
        out["nao_full_mtd"] = max(0, out["mtd"] - out["full_mtd"])

    return out


def render_status_banner(hoje: date, ultima_dia: Optional[date], total_hoje: int, total_ontem: int):
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
            st.success(
                f"✅ **Dados atualizados até agora ({hora_atual})** | "
                f"Hoje: **{total_hoje}** pedidos | Ontem: **{total_ontem}** | "
                f"Variação: {delta_emoji} **{fmt_delta(variacao)} ({pct:+.1f}%)**"
            )
        else:
            st.info(f"ℹ️ **Aguardando primeiros pedidos de hoje** ({hoje.strftime('%d/%m/%Y')}). Ontem: **{total_ontem}**")
    elif dias_diferenca == 1:
        st.warning(f"⚠️ **Dados de hoje ainda não chegaram**. Último dia com dados: **{ultima_dia.strftime('%d/%m/%Y')}** (ontem).")
    else:
        st.error(f"❌ **Dados desatualizados!** Último dia com dados: **{ultima_dia.strftime('%d/%m/%Y')}** ({dias_diferenca} dias atrás).")


# ========== APP ==========
st.set_page_config(page_title="Dashboard Single Pet", page_icon="🐾", layout="wide", initial_sidebar_state="expanded")

st.markdown(
    """
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800;900&display=swap');
      * { font-family: 'Inter', -apple-system, sans-serif; }
      .block-container { padding-top: 28px; padding-bottom: 1rem; max-width: 100%; }
      .hr { border: none; height: 2px; background: linear-gradient(90deg,
          rgba(255,230,0,0.3) 0%, rgba(238,77,45,0.3) 50%, rgba(107,114,128,0.3) 100%); margin: 1rem 0; }
      .subtitle { opacity: .75; margin-top: 6px; font-size: 13px; font-weight: 600; }
      .brand{ font-size: 64px; font-weight: 900; line-height: 1; letter-spacing: -1.6px; margin: 0; display:flex; gap: 10px; }
      .brand-single{ color:#fff; padding: 10px 16px; border-radius: 18px; background: rgba(255,255,255,0.06);
        border: 1px solid rgba(255,255,255,0.10); box-shadow: 0 10px 28px rgba(0,0,0,0.28); }
      .brand-pet{ color:#FFE600; text-shadow: 0 8px 28px rgba(255,230,0,0.22); }
      .section-title { font-size: 20px; font-weight: 900; margin-bottom: 14px; }
      .chart-container { background: rgba(255,255,255,0.02); border-radius: 16px; padding: 16px; border: 1px solid rgba(255,255,255,0.06); }
      .mpCard { background: linear-gradient(145deg, rgba(30,30,35,0.95) 0%, rgba(20,20,25,0.95) 100%);
        border: 2px solid rgba(255,255,255,0.08); border-radius: 20px; padding: 20px 18px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }
      .mpTitle { font-size: 22px; font-weight: 900; margin: 0 0 4px 0; }
      .mpSub { opacity: 0.65; font-size: 11px; margin-bottom: 14px; font-weight: 600; }
      .kpi-container { background: rgba(255,255,255,0.03); border-radius: 12px; padding: 14px 12px; margin-bottom: 12px; border: 1px solid rgba(255,255,255,0.06); }
      .kpi-label { font-size: 11px; font-weight: 800; opacity: 0.7; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
      .kpi-value { font-size: 36px; font-weight: 900; line-height: 1; margin-bottom: 4px; }
      .kpi-delta { font-size: 13px; font-weight: 800; padding: 3px 8px; border-radius: 6px; display: inline-block; margin-top: 4px; }
      .kpi-delta.positive { background: rgba(34, 197, 94, 0.2); color: #4ade80; }
      .kpi-delta.negative { background: rgba(239, 68, 68, 0.2); color: #f87171; }
      .kpi-delta.neutral  { background: rgba(156, 163, 175, 0.2); color: #9ca3af; }

      /* pills para FULL */
      .pill {
        display:inline-block;
        padding: 6px 10px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.10);
        background: rgba(255,255,255,0.04);
        font-size: 11px;
        font-weight: 800;
        margin-right: 6px;
        margin-bottom: 8px;
        opacity: 0.95;
      }
      .pill b { font-weight: 900; }
      .pillWrap { margin: 6px 0 8px 0; }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("⚙️ Configurações")
    auto = st.toggle("🔄 Auto atualizar (30s)", value=True)

    if st.button("🔄 Atualizar agora", type="primary", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.subheader("📅 Período Base")
    base_periodo = st.selectbox("Início da análise:", ["01/02/2026", "01/01/2026", "Personalizado"], index=1)

    if base_periodo == "Personalizado":
        hoje_br = datetime.now(TZ_BR).date()
        inicio_base = st.date_input("Data inicial:", value=date(2026, 1, 1), max_value=hoje_br)
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
        f"**Fonte:** Supabase ({TABLE})\n\n"
        f"**Filtro:** codigo_produto in ('PEDIDO','__PEDIDO__')\n\n"
        f"**Volume:** DISTINCT numero_ecommerce\n\n"
        f"**Data:** data_pedido (operacional)\n\n"
        f"**Timezone:** {TZ_BR}\n\n"
        f"**Produção:** {VERCEL_URL}"
    )

agora_br = datetime.now(TZ_BR)
hoje = agora_br.date()
hora_atual = agora_br.strftime("%H:%M:%S")

with st.spinner("🔄 Carregando dados do Supabase..."):
    try:
        df = fetch_resumo(inicio_base, hoje)
    except Exception as e:
        st.error(f"❌ Erro ao buscar dados: {e}")
        df = pd.DataFrame()

ultima_dia = last_data_disponivel(df)

st.markdown(
    f"""
    <div>
      <div class="brand">
        <span class="brand-single">Single</span>
        <span class="brand-pet">Pet</span>
      </div>
      <div class="subtitle">
        <b>HOJE</b>: {hoje.strftime('%d/%m/%Y')} {hora_atual} (BR) •
        <b>Último dia com dados</b>: {(ultima_dia.strftime('%d/%m/%Y') if ultima_dia else "—")} •
        <b>Volume</b>: DISTINCT numero_ecommerce
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

ontem = hoje - timedelta(days=1)
total_hoje = count_range(df, hoje, hoje)
total_ontem = count_range(df, ontem, ontem)
render_status_banner(hoje, ultima_dia, total_hoje, total_ontem)

if debug_mode:
    st.info(
        f"DEBUG: linhas={len(df)} | unicos={df['numero_ecommerce'].nunique() if not df.empty else 0} | "
        f"min={df['d'].min().date() if (not df.empty and pd.notna(df['d'].min())) else None} | "
        f"max={df['d'].max().date() if (not df.empty and pd.notna(df['d'].max())) else None}"
    )

st.markdown("<div class='hr'></div>", unsafe_allow_html=True)

data_ml = mp_metrics(df, hoje, "Mercado Livre")
data_shp = mp_metrics(df, hoje, "Shopee")
data_out = mp_metrics(df, hoje, "Outros")


def render_mp_card(col, mp: str, emoji: str, d: Dict[str, Any], hoje: date, ontem: date):
    with col:
        st.markdown("<div class='mpCard'>", unsafe_allow_html=True)
        st.markdown(f"<div class='mpTitle'>{emoji} {mp}</div>", unsafe_allow_html=True)
        st.markdown(
            f"<div class='mpSub'>Período: Hoje ({hoje.strftime('%d/%m')}) • Ontem ({ontem.strftime('%d/%m')}) • 7 Dias • Acumulado do mês</div>",
            unsafe_allow_html=True,
        )

        # ✅ detalhamento FULL apenas no card do ML
        if mp == "Mercado Livre":
            full_hoje = int(d.get("full_hoje", 0))
            full_7d = int(d.get("full_7d", 0))
            full_mtd = int(d.get("full_mtd", 0))

            nao_full_hoje = int(d.get("nao_full_hoje", max(0, d["hoje"] - full_hoje)))
            nao_full_7d = int(d.get("nao_full_7d", max(0, d["d7"] - full_7d)))
            nao_full_mtd = int(d.get("nao_full_mtd", max(0, d["mtd"] - full_mtd)))

            st.markdown(
                f"""
                <div class='pillWrap'>
                  <span class='pill'>🚚 FULL hoje: <b>{full_hoje:,}</b></span>
                  <span class='pill'>📭 Não-FULL hoje: <b>{nao_full_hoje:,}</b></span><br>
                  <span class='pill'>🚚 FULL 7D: <b>{full_7d:,}</b></span>
                  <span class='pill'>📭 Não-FULL 7D: <b>{nao_full_7d:,}</b></span><br>
                  <span class='pill'>🚚 FULL mês: <b>{full_mtd:,}</b></span>
                  <span class='pill'>📭 Não-FULL mês: <b>{nao_full_mtd:,}</b></span>
                </div>
                """,
                unsafe_allow_html=True,
            )

        delta_class = "positive" if d["delta_dia"] > 0 else ("negative" if d["delta_dia"] < 0 else "neutral")
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

        cA, cB = st.columns(2)
        with cA:
            delta_7d_class = "positive" if d["delta_7d"] > 0 else ("negative" if d["delta_7d"] < 0 else "neutral")
            st.markdown(
                f"""
                <div class='kpi-container'>
                    <div class='kpi-label'>Últimos 7 dias</div>
                    <div class='kpi-value' style='font-size: 28px;'>{d["d7"]:,}</div>
                    <span class='kpi-delta {delta_7d_class}' style='font-size: 11px;'>
                        {fmt_delta(d["delta_7d"])} ({d["pct_7d"]:+.1f}%)
                    </span>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with cB:
            st.markdown(
                f"""
                <div class='kpi-container'>
                    <div class='kpi-label'>Acumulado do mês (até hoje)</div>
                    <div class='kpi-value' style='font-size: 28px;'>{d["mtd"]:,}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        fig = create_comparison_chart(d["hoje"], d["ontem"], d["d7"], d["prev7"], mp)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        st.markdown("</div>", unsafe_allow_html=True)


c1, c2, c3 = st.columns(3)
render_mp_card(c1, "Mercado Livre", "🟡", data_ml, hoje, ontem)
render_mp_card(c2, "Shopee", "🟠", data_shp, hoje, ontem)
render_mp_card(c3, "Outros", "⚪", data_out, hoje, ontem)

st.markdown("<div class='hr'></div>", unsafe_allow_html=True)

st.markdown("<div class='section-title'>📈 Análise de Tendências</div>", unsafe_allow_html=True)
col_trend, col_dist = st.columns([2.5, 1])

with col_trend:
    st.markdown("<div class='chart-container'>", unsafe_allow_html=True)
    st.markdown(f"**Evolução diária — Últimos {dias_tendencia} dias**")
    tstart = max(inicio_base, hoje - timedelta(days=dias_tendencia - 1))
    pv = daily_pivot(df, tstart, hoje)
    st.plotly_chart(create_trend_chart(pv), use_container_width=True, config={"displayModeBar": False})
    st.markdown("</div>", unsafe_allow_html=True)

with col_dist:
    st.markdown("<div class='chart-container'>", unsafe_allow_html=True)
    st.markdown("**Distribuição — Últimos 7 dias**")
    st.plotly_chart(
        create_donut_chart(data_ml["d7"], data_shp["d7"], data_out["d7"]),
        use_container_width=True,
        config={"displayModeBar": False},
    )
    st.markdown("</div>", unsafe_allow_html=True)

st.markdown("<div class='hr'></div>", unsafe_allow_html=True)
st.markdown("<div class='section-title'>💡 Insights (resumo rápido)</div>", unsafe_allow_html=True)

col_i1, col_i2, col_i3, col_i4 = st.columns(4)

total_periodo = int(df["numero_ecommerce"].nunique()) if not df.empty else 0
mtd_ini = date(hoje.year, hoje.month, 1)
total_mes = count_range(df, mtd_ini, hoje)

with col_i1:
    st.metric("Total hoje", f"{total_hoje:,}", f"{fmt_delta(total_hoje - total_ontem)} vs ontem")
with col_i2:
    st.metric("Total ontem", f"{total_ontem:,}", "dia fechado")
with col_i3:
    st.metric("Acumulado do mês", f"{total_mes:,}", f"de {mtd_ini.strftime('%d/%m')}")
with col_i4:
    st.metric("Total do período base", f"{total_periodo:,}", f"desde {inicio_base.strftime('%d/%m/%Y')}")

st.markdown("<div class='hr'></div>", unsafe_allow_html=True)
st.caption(
    f"🔐 **Fonte:** Supabase ({TABLE}) | "
    f"**Filtro:** codigo_produto in ('PEDIDO','__PEDIDO__') | "
    f"**Volume:** DISTINCT numero_ecommerce | "
    f"**Data:** data_pedido | "
    f"**Atualização:** 30s | "
    f"**Produção:** {VERCEL_URL}"
)

if auto:
    time.sleep(30)
    st.rerun()
