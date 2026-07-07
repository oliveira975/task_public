import hashlib
import io
import re
from datetime import datetime

import pandas as pd
import streamlit as st
from supabase import create_client, Client

st.set_page_config(page_title="Gestão de Tasks por Andar", layout="wide")

# ------------------------------------------------------------------
# Conexão com o Supabase
# ------------------------------------------------------------------
@st.cache_resource
def get_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_ANON_KEY"]
    return create_client(url, key)


supabase = get_supabase()


def _to_native(valor):
    """Converte valores vindos do pandas (numpy.int64, numpy.float64,
    numpy.bool_, NaN/NaT) para tipos nativos do Python, evitando erros
    de serialização JSON ao enviar para o Supabase."""
    if pd.isna(valor):
        return None
    if hasattr(valor, "item"):
        # numpy.int64, numpy.float64, numpy.bool_, etc. têm o método
        # .item() que devolve o equivalente nativo do Python.
        return valor.item()
    return valor

# ------------------------------------------------------------------
# Regex equivalente à do Apps Script original, com o bug do [M|T]
# corrigido para reconhecer corretamente os prefixos M1/M2/M3/MT.
# ------------------------------------------------------------------
REGEX_TASK = re.compile(
    r"(Ready For Assignment|In Progress|Blocked)[\s\S]*?"
    r"(WRPICK\d+|PICK\d+)[\s\S]*?"
    r"((?:M[1-3]|MT)-\d+-\d+-[A-Z]-\d+)[\s\S]*?"
    r"(Adidas_[\w_]+)"
)


def parse_texto(texto: str):
    """Extrai (status, task, source_location, transaction) do texto colado,
    ignorando tasks repetidas dentro do mesmo texto."""
    vistos = set()
    registros = []
    for m in REGEX_TASK.finditer(texto):
        status, task, location, transacao = m.groups()
        if task in vistos:
            continue
        vistos.add(task)
        andar = location.split("-")[0]
        registros.append(
            {
                "status": status,
                "task": task,
                "source_location": location,
                "transaction": transacao,
                "andar": andar,
                "priority": None,
                "qtd_itens": None,
                "observacao": "",
                "concluida": False,
            }
        )
    return registros


def parse_tabela(texto: str):
    """Lê o formato tabular colado direto da planilha/relatório, com
    cabeçalho: Status, Task, Priority, Source location, No. Of task
    details, Transaction (separados por tab). Retorna uma lista de dicts
    já no formato usado pela tabela `tasks`."""
    texto_limpo = texto.strip().strip('"')
    df_colado = pd.read_csv(
        io.StringIO(texto_limpo), sep="\t", dtype=str, keep_default_na=False
    )
    df_colado.columns = [c.strip() for c in df_colado.columns]

    colunas_esperadas = {
        "Status",
        "Task",
        "Priority",
        "Source location",
        "No. Of task details",
        "Transaction",
    }
    faltando = colunas_esperadas - set(df_colado.columns)
    if faltando:
        raise ValueError(f"Colunas faltando no texto colado: {', '.join(faltando)}")

    vistos = set()
    registros = []
    for _, linha in df_colado.iterrows():
        task = linha["Task"].strip()
        if not task or task in vistos:
            continue
        vistos.add(task)
        location = linha["Source location"].strip()
        registros.append(
            {
                "status": linha["Status"].strip(),
                "task": task,
                "source_location": location,
                "transaction": linha["Transaction"].strip(),
                "andar": location.split("-")[0] if location else "Outros",
                "priority": int(linha["Priority"]) if linha["Priority"].strip() else None,
                "qtd_itens": int(linha["No. Of task details"])
                if linha["No. Of task details"].strip()
                else None,
                "observacao": "",
                "concluida": False,
            }
        )
    return registros


def mover_para_historico(rows):
    """Move uma lista de rows (dicts com 'id') da tabela `tasks` para
    `historico_tasks` em lote: insere tudo no histórico primeiro e só
    depois apaga da tabela principal.

    Os valores são normalizados com `_to_native` porque, quando o dict
    vem de um DataFrame do pandas (ex.: `row.to_dict()`), campos como
    `id`, `priority` e `qtd_itens` chegam como numpy.int64/float64, que
    o cliente do Supabase não consegue serializar em JSON — isso fazia
    a chamada falhar silenciosamente antes do st.rerun() rodar, e por
    isso a task não sumia da lista de pendentes."""
    if not rows:
        return
    linhas = [{k: _to_native(v) for k, v in r.items()} for r in rows]
    ids = [r["id"] for r in linhas]
    historico = []
    for r in linhas:
        item = {k: v for k, v in r.items() if k != "id"}
        item["concluida"] = True
        item["concluida_em"] = datetime.now().isoformat()
        item["turno_fechado_em"] = None
        historico.append(item)
    supabase.table("historico_tasks").insert(historico).execute()
    supabase.table("tasks").delete().in_("id", ids).execute()


def salvar_registros(registros):
    """Filtra tasks que já existem no banco e insere só as novas."""
    if not registros:
        return 0
    existentes = supabase.table("tasks").select("task").execute().data
    tasks_existentes = {r["task"] for r in existentes}
    novos = [r for r in registros if r["task"] not in tasks_existentes]
    if novos:
        supabase.table("tasks").insert(novos).execute()
    return len(novos)


# ------------------------------------------------------------------
# Login (Supabase Auth - email/senha)
# ------------------------------------------------------------------
def tela_login():
    st.title("🔐 Login")
    with st.form("login_form"):
        email = st.text_input("Email")
        senha = st.text_input("Senha", type="password")
        entrar = st.form_submit_button("Entrar")

    if entrar:
        try:
            res = supabase.auth.sign_in_with_password(
                {"email": email, "password": senha}
            )
            st.session_state["user"] = res.user
            st.session_state["session"] = res.session
            st.rerun()
        except Exception as e:
            st.error(f"Falha no login: {e}")


def logout():
    supabase.auth.sign_out()
    st.session_state.pop("user", None)
    st.session_state.pop("session", None)
    st.rerun()


if "user" not in st.session_state:
    tela_login()
    st.stop()

# ------------------------------------------------------------------
# App principal (usuário já autenticado)
# ------------------------------------------------------------------
st.sidebar.write(f"👤 {st.session_state['user'].email}")
if st.sidebar.button("Sair"):
    logout()

st.title("📦 Gestão de Tasks por Andar")

# --- Importar novas tasks (texto corrido, com form que limpa sozinho) ---
with st.expander("➕ Importar novas tasks (colar texto bruto)", expanded=False):
    with st.form("form_texto_bruto", clear_on_submit=True):
        texto_bruto = st.text_area("Cole aqui o texto copiado do sistema", height=200)
        enviar_texto = st.form_submit_button("Processar e salvar")

    if enviar_texto:
        if not texto_bruto.strip():
            st.warning("Cole algum conteúdo antes de processar.")
        else:
            registros = parse_texto(texto_bruto)
            if not registros:
                st.warning("Nenhuma task reconhecida no texto colado.")
            else:
                qtd_novos = salvar_registros(registros)
                if qtd_novos:
                    st.success(f"{qtd_novos} nova(s) task(s) salva(s).")
                else:
                    st.info("Todas as tasks coladas já existiam.")
                st.rerun()

# --- Importar tabela colada (formato com cabeçalho: Status, Task, Priority,
#     Source location, No. Of task details, Transaction) ---
with st.expander("📋 Colar tabela (com cabeçalho, separada por tab)", expanded=False):
    st.caption(
        "Cole aqui o bloco copiado da planilha/relatório, com a linha de "
        "cabeçalho incluída (Status, Task, Priority, Source location, "
        "No. Of task details, Transaction)."
    )
    with st.form("form_tabela", clear_on_submit=True):
        tabela_bruta = st.text_area("Cole a tabela aqui", height=220)
        enviar_tabela = st.form_submit_button("Processar tabela e salvar")

    if enviar_tabela:
        if not tabela_bruta.strip():
            st.warning("Cole algum conteúdo antes de processar.")
        else:
            try:
                registros = parse_tabela(tabela_bruta)
            except Exception as e:
                st.error(f"Não foi possível interpretar a tabela: {e}")
                registros = []

            if registros:
                qtd_novos = salvar_registros(registros)
                if qtd_novos:
                    st.success(f"{qtd_novos} nova(s) task(s) salva(s) a partir da tabela.")
                else:
                    st.info("Todas as tasks coladas já existiam.")
                st.rerun()
            elif tabela_bruta.strip():
                st.warning("Nenhuma linha de task reconhecida na tabela colada.")

# --- Menu lateral: navegar entre Andares (tela principal) e Histórico ---
st.sidebar.divider()
pagina = st.sidebar.radio("📌 Navegação", ["📦 Andares", "🗂️ Histórico"])

if pagina == "📦 Andares":
    # --- Carregar dados do turno atual ---
    dados = supabase.table("tasks").select("*").execute().data
    df = pd.DataFrame(dados)

    andares = sorted(df["andar"].unique()) if not df.empty else []
    if not andares:
        st.info("Nenhuma task pendente cadastrada. Importe novas tasks acima ou confira o Histórico.")

    abas = st.tabs([f"Andar {a}" for a in andares]) if andares else []

    # --- Abas por andar (só tasks ainda não concluídas) ---
    for aba, andar in zip(abas, andares):
        with aba:
            df_andar = df[df["andar"] == andar]
            transacoes = sorted(df_andar["transaction"].unique())

            for transacao in transacoes:
                col_titulo, col_botao = st.columns([4, 1])
                with col_titulo:
                    st.subheader(transacao)
                df_t = df_andar[df_andar["transaction"] == transacao].sort_values("task")

                with col_botao:
                    if st.button(
                        f"✅ Concluir todas ({len(df_t)})",
                        key=f"concluir_todas_{andar}_{transacao}",
                    ):
                        rows_para_mover = []
                        for _, row in df_t.iterrows():
                            item = row.to_dict()
                            obs_key = f"obs_{row['id']}"
                            item["observacao"] = st.session_state.get(
                                obs_key, item.get("observacao") or ""
                            )
                            rows_para_mover.append(item)
                        try:
                            mover_para_historico(rows_para_mover)
                            st.rerun()
                        except Exception as e:
                            st.error(f"Não foi possível concluir as tasks: {e}")

                pendentes_tasks = []
                selecionados = []
                for _, row in df_t.iterrows():
                    extras = []
                    if pd.notna(row.get("priority")):
                        extras.append(f"prioridade {int(row['priority'])}")
                    if pd.notna(row.get("qtd_itens")):
                        extras.append(f"{int(row['qtd_itens'])} item(ns)")
                    sufixo_extra = f" [{', '.join(extras)}]" if extras else ""
                    label = (
                        f"{row['task']} — {row['transaction']} "
                        f"(origem: {row['source_location']}){sufixo_extra}"
                    )

                    col_chk, col_obs = st.columns([3, 2])

                    with col_chk:
                        marcado = st.checkbox(label, value=False, key=f"chk_{row['id']}")

                    with col_obs:
                        observacao_atual = row.get("observacao") or ""
                        nova_observacao = st.text_input(
                            "Observação",
                            value=observacao_atual,
                            key=f"obs_{row['id']}",
                            label_visibility="collapsed",
                            placeholder="Observação (ex.: motivo de não concluir)...",
                        )
                        if nova_observacao != observacao_atual:
                            supabase.table("tasks").update(
                                {"observacao": nova_observacao}
                            ).eq("id", row["id"]).execute()

                    # A lista de "Tasks pendentes (copiar)" reflete só o
                    # estado ATUAL do checkbox nesta renderização: assim
                    # que marcado, a task já não entra na lista abaixo,
                    # independente do envio ao banco ter terminado ou não.
                    if marcado:
                        item = row.to_dict()
                        item["observacao"] = nova_observacao
                        selecionados.append(item)
                    else:
                        pendentes_tasks.append(row["task"])

                if pendentes_tasks:
                    # A key inclui um hash do conteúdo da lista: assim,
                    # sempre que um checkbox muda e a lista de pendentes
                    # muda, a key também muda, forçando o Streamlit a
                    # recriar o widget com o novo `value=` em vez de
                    # reaproveitar o valor antigo salvo em session_state
                    # (que teria prioridade sobre `value=` se a key fosse fixa).
                    conteudo_pendentes = ", ".join(pendentes_tasks)
                    hash_pendentes = hashlib.md5(
                        conteudo_pendentes.encode("utf-8")
                    ).hexdigest()[:8]
                    st.text_area(
                        "📋 Tasks pendentes (copiar):",
                        value=conteudo_pendentes,
                        height=80,
                        key=f"copiar_{andar}_{transacao}_{hash_pendentes}",
                    )

                # Só depois de desenhar a lista de pendentes (já sem os
                # marcados) é que efetivamente movemos as tasks marcadas
                # para o histórico e limpamos a tabela `tasks`.
                if selecionados:
                    try:
                        mover_para_historico(selecionados)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Não foi possível concluir a(s) task(s): {e}")

                st.divider()

else:
    # --- Página Histórico: tasks concluídas individualmente + snapshots de
    #     fechamento de turno, agrupadas por dia (dd/mm/aaaa) ---
    st.title("🗂️ Histórico")

    hist_dados = supabase.table("historico_tasks").select("*").execute().data
    df_hist = pd.DataFrame(hist_dados)

    if df_hist.empty:
        st.info("Nenhum registro no histórico ainda.")
    else:
        df_hist["data_evento"] = pd.to_datetime(
            df_hist["concluida_em"].fillna(df_hist["turno_fechado_em"])
        )
        df_hist["dia"] = df_hist["data_evento"].dt.strftime("%d/%m/%Y")
        df_hist = df_hist.sort_values("data_evento", ascending=False)

        busca = st.text_input("🔎 Buscar por task", key="busca_historico")
        if busca:
            df_hist = df_hist[df_hist["task"].str.contains(busca, case=False, na=False)]

        colunas_exibir = [
            "task",
            "transaction",
            "source_location",
            "andar",
            "priority",
            "qtd_itens",
            "observacao",
            "concluida",
            "data_evento",
        ]
        colunas_exibir = [c for c in colunas_exibir if c in df_hist.columns]

        if df_hist.empty:
            st.info("Nenhum registro encontrado para essa busca.")
        else:
            # "dia" já sai em ordem decrescente por causa do sort acima
            dias = df_hist["dia"].drop_duplicates().tolist()
            for dia in dias:
                df_dia = df_hist[df_hist["dia"] == dia]
                with st.expander(f"📅 {dia} — {len(df_dia)} registro(s)", expanded=False):
                    st.dataframe(
                        df_dia[colunas_exibir], use_container_width=True, hide_index=True
                    )

        # --- Limpar histórico ---
        st.divider()
        st.subheader("🗑️ Limpar histórico")
        st.caption(
            "Apaga PERMANENTEMENTE todos os registros da tabela `historico_tasks`. "
            "As tasks pendentes (tela de Andares) não são afetadas."
        )
        confirmar_limpeza = st.checkbox(
            "Confirmo que quero apagar todo o histórico (ação irreversível)",
            key="confirmar_limpeza_historico",
        )
        if st.button("🗑️ Limpar todo o histórico", disabled=not confirmar_limpeza):
            supabase.table("historico_tasks").delete().neq("id", 0).execute()
            st.success("Histórico limpo com sucesso!")
            st.rerun()

# ------------------------------------------------------------------
# Fechar turno: salva histórico completo e intacto ANTES de limpar
# (só afeta as tasks que ainda estão pendentes, já que as concluídas
# já foram movidas para o Histórico individualmente ao marcar o checkbox)
# ------------------------------------------------------------------
st.sidebar.divider()
if st.sidebar.button("🔒 Fechar turno e gerar histórico"):
    try:
        dados_restantes = supabase.table("tasks").select("*").execute().data
        if dados_restantes:
            agora = datetime.now().isoformat()
            historico = []
            for r in dados_restantes:
                item = {k: v for k, v in r.items() if k != "id"}
                item["turno_fechado_em"] = agora
                item["concluida_em"] = None
                historico.append(item)

            # 1. Backup completo primeiro
            supabase.table("historico_tasks").insert(historico).execute()

            # 2. Só depois de confirmado o backup, limpa o turno atual
            supabase.table("tasks").delete().neq("id", 0).execute()

        st.success("Turno fechado, histórico salvo e tasks limpas!")
        st.rerun()
    except Exception as e:
        st.error(
            f"ERRO ao fechar turno — nada foi apagado, pois o backup falhou: {e}"
        )