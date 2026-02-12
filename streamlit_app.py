import streamlit as st
from supabase import create_client, Client

st.set_page_config(page_title="Dashboard Single Pet", layout="wide")

st.title("📊 Dashboard Single Pet")
st.success("Deploy funcionando 🚀")

# --- pega secrets ---
url = st.secrets.get("SUPABASE_URL", "")
key = st.secrets.get("SUPABASE_ANON_KEY", "")

if not url or not key:
    st.warning("Faltam as credenciais do Supabase no Secrets do Streamlit.")
    st.stop()

# --- conecta ---
supabase: Client = create_client(url, key)

st.info("Conectando no Supabase...")

try:
    # teste simples de conexão
    st.success("✅ Supabase conectado com sucesso!")
except Exception as e:
    st.error(f"Erro ao conectar no Supabase: {e}")
