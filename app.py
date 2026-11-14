"""Interface Streamlit do Agente Analista Eleitoral 2026.  Uso: 7_interface.bat (streamlit run app.py)"""
import uuid

import streamlit as st

import agente
import config

st.set_page_config(page_title="Analista Eleitoral 2026", layout="wide")


@st.cache_resource
def obter_agente():
    return agente.criar_agente()          # mesmo agente do notebook: ferramentas + memória SQLite


if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.mensagens = []

with st.sidebar:
    st.header("Analista Eleitoral 2026")
    st.caption(f"Deputado Federal — SP (70 vagas) e BA (39 vagas)\n\nModelo: {config.MODELO_AGENTE}")
    if st.button("Nova conversa"):
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.mensagens = []
        st.rerun()
    verificar = st.toggle("Verificação automática", value=True)
    st.markdown("**Exemplos**")
    exemplos = ["Por que o Rui Falcão não foi eleito em SP?", "Quem foi o último eleito do PL em SP?",
                "O que diz o art. 108 do Código Eleitoral?", "Quais os 5 mais votados na Bahia?"]
    escolhido = next((e for e in exemplos if st.button(e)), None)
    st.caption(f"Conversa: {st.session_state.thread_id[:8]}")

for m in st.session_state.mensagens:
    with st.chat_message(m["papel"]):
        st.markdown(m["texto"])
        if m.get("detalhes"):
            with st.expander("Ferramentas e verificação"):
                st.markdown(m["detalhes"])

pergunta = st.chat_input("Pergunte sobre a eleição de Deputado Federal 2026 em SP ou BA") or escolhido
if pergunta:
    st.session_state.mensagens.append({"papel": "user", "texto": pergunta})
    with st.chat_message("user"):
        st.markdown(pergunta)
    with st.chat_message("assistant"):
        with st.spinner("Consultando as ferramentas..."):
            try:
                r = agente.perguntar(obter_agente(), pergunta, st.session_state.thread_id, verificar=verificar)
                texto = r.texto
                v = r.verificacao
                detalhes = (f"- Ferramentas: {' → '.join(r.ferramentas) or 'nenhuma'}\n"
                            f"- Verificação: {v.resumo() if v else 'desligada'}"
                            f"{' (resposta reescrita)' if r.revisada else ''}\n- Tempo: {r.segundos} s")
                if v and v.problemas:
                    detalhes += "\n- Problemas: " + "; ".join(v.problemas)
            except Exception as erro:  # noqa: BLE001
                texto, detalhes = f"Erro: {erro.__class__.__name__}: {erro}", ""
        st.markdown(texto)
        if detalhes:
            with st.expander("Ferramentas e verificação"):
                st.markdown(detalhes)
    st.session_state.mensagens.append({"papel": "assistant", "texto": texto, "detalhes": detalhes})
