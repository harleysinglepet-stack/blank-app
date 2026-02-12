# app.py — DASHBOARD PROFISSIONAL SINGLE PET v2.3 (VOLUME REAL = DISTINCT)
# ✅ Correções:
# - Compatível com Streamlit Secrets (secrets.toml) E variáveis de ambiente (Vercel/.env)
# - CRON_SECRET pega pelo nome correto: ENV "CRON_SECRET" (fallback de st.secrets["supabase"]["cron_secret"])
# - Remove "width=stretch" (Streamlit não aceita) -> use_container_width=True
# - Paginação robusta + anti-cache
# - Parse robusto DATE vs TIMESTAMP com timezone BR
# - Volume real = DISTINCT numero_ecommerce
#
# 🔧 AJUSTES RÁPIDOS (se precisar):
# - TABLE, COL_* e FILTRO_PEDIDO (se seu banco for diferente)

from __future__ import annotations

import os
import time
from datetime import date, timedelta, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# =======================
# CONFIGURAÇÕES PRINCIPAIS
# =======================
HTTP_TIMEOUT = 30
TZ_BR = ZoneInfo("America/Sao_Paulo")
VERCEL_URL = "https://singlepet-dashboard.vercel.app"

# Tabela / colunas esperadas
TABLE = "pedidos_tiny"
COL_NUMERO = "numero_ecommerce"
COL_DATA = "data_pedido"
COL_MP = "marketplace"
COL_CODIGO = "codigo_produto"

# Paginação PostgREST
PAGE_SIZE = 1000

# ⚠️ Se esse filtro estiver escondendo vendas, mude para False
FILTRO_PEDIDO = True  # True = filtra codigo_produto em (PEDIDO, __PEDIDO__)
VALORES_PEDIDO = ("PEDIDO", "__PEDIDO__")

# =======================
# CORES
# =======================
MP_COLORS = {
    "Mercado Livre": {"primary": "#FFE600", "secondary": "#FFD700"},
    "Shopee": {"primary": "#EE4D2D", "secondary": "#FF6B4A"},
    "Outros": {"primary": "#6B7280", "secondary": "#9CA3AF"},
}
MPS = ["Mercado Livre", "Shopee", "Outros"]


# =======================
# SECRETS (st.secrets OU ENV)
# =======================
def get_secret_st_or_env(st_path: List[str], env_key: str, required: bool = True) -> str:
    """
    1) tenta st.secrets (Streamlit)
    2) fallback para ENV (Vercel / .env)
    """
    # 1) st.secrets
    try:
        cur: Any = st.secrets
        for k in st_path:
            cur = cur.get(k, None)
            if cur is None:
                raise KeyError
        val = str(cur).strip()
        if val:
            return val
    except Exception:
        pass

    # 2) ENV
    val = os.getenv(env_key, "").strip()
    if val:
        return val

    if required:
        st.error(f"❌ Secret não encontrado: {env_key}")
        st.stop()

    return ""


SUPABASE_URL = get_secret_st_or_env(["supabase", "url"], "SUPABASE_URL", required=True)
SUPABASE_KEY = get_secret_st_or_env(["supabase", "anon_key"], "SUPABASE_ANON_KEY", required=True)

# ✅ Nome correto que você pediu: CRON_SECRET
# (fallback opcional do toml: [supabase] cron_secret = "...")
CRON_SECRET = get_secret_st_or_env(["supabase", "cron_secret"], "CRON_SECRET", required=False)

BUSCA_TINY_URL = f"{SUPABASE_URL.rstrip('/')}/functions/v1/BUSCA-TINY"


# =======================
# HELPERS
# =======================
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
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }


def trigger_busca_tiny_import(max_dias: int = 1, incluir_hoje: bool = True) -> Dict[str, Any]:
    """
    Dispara BUSCA-TINY.
    Requer CRON_SECRET configurado (ENV CRON_SECRET ou secrets [supabase].cron_secret).
    """
    if not CRON_SECRET:
        return {"ok": False, "erro": "CRON_SECRET não configurado (ENV CRON_SECRET ou secrets.toml)."}

    headers = {"Content-Type": "application/json", "x-cron-secret": CRON_SECRET}
    payload = {"modo": "resumo_por_dia", "maxDiasPorExec": int(max_dias), "incluirHoje": bool(incluir_hoje)}

    try:
        r = requests.post(BUSCA_TINY_URL, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:800]}

        if r.status_code not in (200, 201):
            return {"ok": False, "erro": f"BUSCA-TINY HTTP {r.status_code}", "resposta": data}

        return data if isinstance(data, dict) else {"ok": True, "data": data}

    except Exception as e:
        return {"ok": False, "erro": str(e)}


# =======================
# FETCH (CACHE)
# =======================
@st.cache_data(ttl=30, show_spinner=False)
def fetch_resumo(d1: date, d2: date) -> pd.DataFrame:
    """
    Busca pedidos filtrados por data base (data_pedido).
    - Range: [>= d1, < d2+1]
    - paginação limit/offset
    - parse date vs timestamp
    - volume real: dedup por numero_ecommerce
    """
    select_cols = f"{COL_NUMERO},{COL_DATA},{COL_MP}"
    base = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{TABLE}"

    dt1 = iso(d1)
    dt2_exclusive = iso(d2 + timedelta(days=1))

    # Monta URL base
    url = f"{base}?select={select_cols}&{COL_DATA}=gte.{dt1}&{COL_DATA}=lt.{dt2_exclusive}&order={COL_DATA}.asc&limit={PAGE_SIZE}"

    # Filtro opcional PEDIDO
    if FILTRO_PEDIDO:
        vals = ",".join(VALORES_PEDIDO)
        url += f"&{COL_CODIGO}=in.({vals})"

    rows: List[Dict[str, Any]] = []
    offset = 0

    while True:
        paginated_url = f"{url}&offset={offset}&_ts={int(time.time())}"
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

    # valida colunas
    for c in [COL_NUMERO, COL_DATA, COL_MP]:
        if c not in df.columns:
            raise RuntimeError(f"Coluna ausente no retorno do Supabase: {c}")

    # Parse data robusto
    s = df[COL_DATA].astype(str).str.strip()
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

    # Marketplace
    df[COL_MP] = df[COL_MP].apply(normalize_marketplace)
    df.loc[~df[COL_MP].isin(MPS), COL_MP] = "Outros"

    # Dedup por numero_ecommerce
    df[COL_NUMERO] = df[COL_NUMERO].astype(str).str.strip()
    df = df[df[COL_NUMERO].ne("")].copy()
    df = df.drop_duplicates(subset=[COL_NUMERO], keep="first").reset_index(drop=True)

    return df


def last_data_disponivel(df: pd.DataFrame) -> Optional[date]:
    if df.empty:
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
        x = x[x[COL_MP] == mp]

    d1_ts = pd.Timestamp(d1).normalize()
    d2_ts = pd.Timestamp(d2).normalize()
    m = (x["d"] >= d1_ts) & (x["d"] <= d2_ts)

    return int(x.loc[m, COL_NUMERO].nunique())


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

    g = df.groupby(["d", COL_MP])[COL_NUMERO].nunique().reset_index(name="qtd")
    pv = g.pivot(index="d", columns=COL_MP, values="qtd").fillna(0)

    for mp in MPS:
        if mp not in pv.columns:
            pv[mp] = 0

    pv = pv[MPS].sort_index()
    pv.index = pd.to_datetime(pv.index).normalize()
    pv = pv.reindex(idx, fill_value=0)
    return pv


# =======================
# PLOTLY
# =======================
def create_comparison_chart(hoje_val: int, ontem_val: int, d7_val: int, prev7_val: int, mp: str) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=["Hoje", "Ontem"], y=[hoje_val, ontem_val],
        marker_color=MP_COLORS[mp]["primary"],
        text=[hoje_val, ontem_val], textposition="outside",
        name="Dia",
    ))
    fig.add_trace(go.Bar(
        x=["Últimos 7D", "7D Anteriores"], y=[d7_val, prev7_val],
        marker_color=MP_COLORS[mp]["secondary"],
        text=[d7_val, prev7_val], textposition="outside",
        name="Semana",
    ))
    fig.update_layout(
        height=200, margin=dict(l=0, r=0, t=10, b=0),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False
    )
    return fig


def create_trend_chart(pv: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for mp in MPS:
        fig.add_trace(go.Bar(
            x=pv.index, y=pv[mp],
            name=mp, marker_color=MP_COLORS[mp]["primary"],
            hovertemplate="<b>%{x|%d/%m}</b><br>" + mp + ": %{y}<extra></extra>",
        ))
    fig.update_layout(
        barmode="stack",
        height=320, margin=dict(l=20, r=20, t=30, b=20),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="white"),
        hovermode="x unified",
        legend=dict(orientation="h", y=1.02, x=0.5, xanchor="center", yanchor="bottom"),
    )
    return fig


def create_donut_chart(ml: int, shp: int, out: int) -> go.Figure:
    total = ml + shp + out
    if total == 0:
        return go.Figure()

    fig = go.Figure(data=[go.Pie(
        labels=["Mercado Livre", "Shopee", "Outros"],
        values=[ml, shp, out],
        hole=0.6,
        marker=dict(colors=[MP_COLORS[m]["primary"] for m in MPS]),
        textinfo="label+percent"
    )])
    fig.add_annotation(text=f"<b>{total:,}</b><br><span style='font-size:12px'>TOTAL</span>", x=0.5, y=0.5, showarrow=False)
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), paper_bgcolor="rgba(0,0,0,0)", showlegend=False)
    return fig


# =======================
# MÉTRICAS
# =======================
def mp_metrics(df: pd.DataFrame, hoje: date, mp: str) -> Dict[str, Any]:
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

    return dict(
        hoje=hoje_mp, ontem=ontem_mp, delta_dia=delta_dia, pct_dia=pct_dia,
        d7=d7_mp, prev7=prev7_mp, delta_7d=delta_7d, pct_7d=pct_7d, mtd=mtd_mp
    )


# =======================
# UI
# =======================
def render_status_banner(hoje: date, ultima_dia: Optional[date], total_hoje: int, total_ontem: int):
    if ultima_dia is None:
        st.error("❌ Nenhum dado encontrado no período selecionado.")
        return

    dias_dif = (hoje - ultima_dia).days

    if dias_dif == 0:
        if total_hoje > 0:
            hora_atual = datetime.now(TZ_BR).strftime("%H:%M")
            variacao = total_hoje - total_ontem
            pct = pct_change(total_hoje, total_ontem)
            emo = "📈" if variacao > 0 else ("📉" if variacao < 0 else "➡️")
            st.success(f"✅ **Dados atualizados ({hora_atual})** | Hoje: **{total_hoje}** | Ontem: **{total_ontem}** | Variação: {emo} **{fmt_delta(variacao)} ({pct:+.1f}%)**")
        else:
            st.info(f"ℹ️ **Aguardando primeiros pedidos de hoje** ({hoje.strftime('%d/%m/%Y')}). Ontem: **{total_ontem}**.")
    elif dias_dif == 1:
        st.warning(f"⚠️ **Hoje ainda não foi importado**. Último dia com dados: **{ultima_dia.strftime('%d/%m/%Y')}**. Ontem: **{total_ontem}**.")
    else:
        st.error(f"❌ **Dados desatualizados!** Último dia: **{ultima_dia.strftime('%d/%m/%Y')}** (**{dias_dif}** dias atrás).")


def render_mp_card(col, mp: str, emoji: str, d: Dict[str, Any], hoje: date, ontem: date):
    with col:
        st.markdown(f"### {emoji} {mp}")
        st.caption(f"Hoje ({hoje.strftime('%d/%m')}) • Ontem ({ontem.strftime('%d/%m')}) • 7D • MTD")

        delta_class = "normal"
        if d["delta_dia"] > 0:
            delta_class = "✅"
        elif d["delta_dia"] < 0:
            delta_class = "⚠️"

        st.metric("Hoje", d["hoje"], f'{fmt_delta(d["delta_dia"])} ({d["pct_dia"]:+.1f}%) {delta_class}')
        cA, cB = st.columns(2)
        with cA:
            st.metric("7 Dias", d["d7"], f'{fmt_delta(d["delta_7d"])} ({d["pct_7d"]:+.1f}%)')
        with cB:
            st.metric("MTD", d["mtd"])

        fig = create_comparison_chart(d["hoje"], d["ontem"], d["d7"], d["prev7"], mp)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


# =======================
# APP
# =======================
st.set_page_config(page_title="Dashboard Single Pet", page_icon="🐾", layout="wide")

with st.sidebar:
    st.header("⚙️ Configurações")
    auto = st.toggle("🔄 Auto atualizar (30s)", value=True)

    if st.button("🔄 Atualizar agora (Import Tiny)", type="primary", use_container_width=True):
        with st.spinner("📦 Importando do Tiny (BUSCA-TINY)..."):
            res = trigger_busca_tiny_import(max_dias=2, incluir_hoje=True)

        if isinstance(res, dict) and res.get("ok") is True:
            st.success("✅ Import OK! Atualizando painel...")
        else:
            st.warning(f"⚠️ Import não rodou. Vou apenas atualizar o painel.\n\nDetalhe: {res}")

        st.cache_data.clear()
        st.rerun()

    st.divider()

    st.subheader("📅 Período Base")
    base_periodo = st.selectbox("Início da análise:", ["01/02/2026", "01/01/2026", "Personalizado"], index=1)
    hoje_br = datetime.now(TZ_BR).date()

    if base_periodo == "Personalizado":
        inicio_base = st.date_input("Data inicial:", value=date(2026, 1, 1), max_value=hoje_br)
    elif base_periodo == "01/02/2026":
        inicio_base = date(2026, 2, 1)
    else:
        inicio_base = date(2026, 1, 1)

    st.subheader("📈 Tendência")
    dias_tendencia = st.slider("Dias", 7, 30, 14, 1)

    debug_mode = st.checkbox("🐛 Debug", value=False)

    st.divider()
    st.caption(
        "Secrets aceitos:\n"
        "- ENV: SUPABASE_URL, SUPABASE_ANON_KEY, CRON_SECRET\n"
        "- secrets.toml: [supabase].url, anon_key, cron_secret"
    )

# Header
agora_br = datetime.now(TZ_BR)
hoje = agora_br.date()
hora_atual = agora_br.strftime("%H:%M:%S")

st.markdown("## 🐾 Single Pet — Dashboard de Vendas (Volume)")
st.caption(f"Hoje: {hoje.strftime('%d/%m/%Y')} {hora_atual} (BR) | Fonte: Supabase/{TABLE} | Volume: DISTINCT {COL_NUMERO}")

# Fetch
try:
    with st.spinner("🔄 Carregando dados do Supabase..."):
        df = fetch_resumo(inicio_base, hoje)
except Exception as e:
    st.error("❌ O app quebrou ao buscar dados no Supabase.")
    st.code(str(e))
    st.stop()

ultima_dia = last_data_disponivel(df)

ontem = hoje - timedelta(days=1)
total_hoje = count_range(df, hoje, hoje)
total_ontem = count_range(df, ontem, ontem)

render_status_banner(hoje, ultima_dia, total_hoje, total_ontem)

if debug_mode:
    st.markdown("### 🐛 Debug")
    st.write("Linhas (dedup):", len(df))
    if not df.empty:
        st.write("Min d:", df["d"].min())
        st.write("Max d:", df["d"].max())
        st.write("Pedidos únicos:", df[COL_NUMERO].nunique())
        st.write("MP counts:", df.groupby(COL_MP)[COL_NUMERO].nunique().to_dict())
    st.write("FILTRO_PEDIDO:", FILTRO_PEDIDO, "VALORES:", VALORES_PEDIDO)
    st.write("CRON_SECRET carregado?:", bool(CRON_SECRET))

st.divider()

# Métricas por MP
data_ml = mp_metrics(df, hoje, "Mercado Livre")
data_shp = mp_metrics(df, hoje, "Shopee")
data_out = mp_metrics(df, hoje, "Outros")

c1, c2, c3 = st.columns(3)
render_mp_card(c1, "Mercado Livre", "🟡", data_ml, hoje, ontem)
render_mp_card(c2, "Shopee", "🟠", data_shp, hoje, ontem)
render_mp_card(c3, "Outros", "⚪", data_out, hoje, ontem)

st.divider()

# Tendência + Donut
st.markdown("### 📈 Análise de Tendência")
col_trend, col_dist = st.columns([2.5, 1])

with col_trend:
    tstart = max(inicio_base, hoje - timedelta(days=dias_tendencia - 1))
    pv = daily_pivot(df, tstart, hoje)
    fig_trend = create_trend_chart(pv)
    st.plotly_chart(fig_trend, use_container_width=True, config={"displayModeBar": False})

with col_dist:
    fig_donut = create_donut_chart(data_ml["d7"], data_shp["d7"], data_out["d7"])
    st.plotly_chart(fig_donut, use_container_width=True, config={"displayModeBar": False})

st.divider()
st.caption(
    f"🔐 Supabase REST | Tabela: {TABLE} | "
    f"Base: {COL_DATA} | Volume: DISTINCT {COL_NUMERO} | "
    f"TZ: {TZ_BR} | Produção: {VERCEL_URL}"
)

# Auto refresh
if auto:
    time.sleep(30)
    st.rerun()
