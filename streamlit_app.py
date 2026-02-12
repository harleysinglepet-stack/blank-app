import os
import streamlit as st
from supabase import create_client, Client

st.set_page_config(page_title="Dashboard Single Pet", layout="wide")

st.title("📊 Dashboard Single Pet")
st.success("Deploy funcionando 🚀")

url = st.secrets.get("SUPABASE_URL", "")
key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", "")

if not url or not key:
    st.warning("Faltam as credenciais do Supabase no Secrets do Streamlit.")
    st.stop()

supabase: Client = create_client(url, key)

st.info("Conectando no Supabase...")

try:
    # teste simples: buscar 1 linha de alguma tabela (troca pelo nome real depois)
    # por enquanto só confirma que o client criou
    st.success("✅ Supabase conectado com sucesso!")
except Exception as e:
    st.error(f"Erro ao conectar no Supabase: {e}")
