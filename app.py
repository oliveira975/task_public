import hashlib
import io
import re
from datetime import datetime, timedelta

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


@st.cache_resource
def get_supabase_admin():
    """Cliente com a service_role key, usado só na página Admin para
    gerenciar contas (criar, trocar senha, ativar/desativar). Retorna
    None se a chave não estiver configurada em secrets.toml, para o app
    inteiro continuar funcionando normalmente sem essa página."""
    url = st.secrets.get("SUPABASE_URL")
    service_key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not service_key:
        return None
    return create_client(url, service_key)


supabase = get_supabase()

# E-mails com acesso à página Admin (lista separada por vírgula em secrets.toml)
_ADMIN_EMAILS_RAW = st.secrets.get("ADMIN_EMAILS", "")
ADMIN_EMAILS = {e.strip().lower() for e in _ADMIN_EMAILS_RAW.split(",") if e.strip()}


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


_MAPA_COLUNAS_CANONICAS = {
    "status": "Status",
    "task": "Task",
    "priority": "Priority",
    "source location": "Source location",
    "no. of task details": "No. Of task details",
    "transaction": "Transaction",
}


def _normalizar_colunas(df_colado):
    """Renomeia as colunas do DataFrame colado para os nomes canônicos
    esperados pelo app, comparando de forma case-insensitive e ignorando
    espaços nas pontas — assim 'Source Location', 'source location' e
    'SOURCE LOCATION' são todas reconhecidas como a mesma coluna."""
    novas_colunas = []
    for coluna in df_colado.columns:
        chave = coluna.strip().lower()
        novas_colunas.append(_MAPA_COLUNAS_CANONICAS.get(chave, coluna.strip()))
    df_colado.columns = novas_colunas
    return df_colado


def parse_status_task_simples(texto: str):
    """Tenta interpretar o texto colado como uma tabela separada por tab
    contendo pelo menos as colunas 'Status' e 'Task' (ex.: colado direto
    de um relatório só com essas duas colunas, ou com mais colunas como
    Source Location/Transaction). As colunas Priority, Source location,
    No. Of task details e Transaction são opcionais — se não vierem,
    ficam com um valor padrão para não violar o schema.

    Retorna None se o texto não parecer estar nesse formato tabular
    (nesse caso quem chamou deve cair de volta para o parser por regex)."""
    texto_limpo = texto.strip().strip('"')
    if "\t" not in texto_limpo:
        return None
    try:
        df_colado = pd.read_csv(
            io.StringIO(texto_limpo), sep="\t", dtype=str, keep_default_na=False
        )
    except Exception:
        return None

    df_colado = _normalizar_colunas(df_colado)
    if "Status" not in df_colado.columns or "Task" not in df_colado.columns:
        return None

    tem_location = "Source location" in df_colado.columns
    tem_transacao = "Transaction" in df_colado.columns
    tem_priority = "Priority" in df_colado.columns
    tem_qtd = "No. Of task details" in df_colado.columns

    vistos = set()
    registros = []
    for _, linha in df_colado.iterrows():
        task = linha["Task"].strip()
        status = linha["Status"].strip()
        if not task or not status or task in vistos:
            continue
        vistos.add(task)

        location = linha["Source location"].strip() if tem_location else ""
        transacao = linha["Transaction"].strip() if tem_transacao else ""
        priority_raw = linha["Priority"].strip() if tem_priority else ""
        qtd_raw = linha["No. Of task details"].strip() if tem_qtd else ""

        registros.append(
            {
                "status": status,
                "task": task,
                "source_location": location,
                "transaction": transacao or "Sem Transação",
                "andar": location.split("-")[0] if location else "Sem Andar",
                "priority": int(priority_raw) if priority_raw.isdigit() else None,
                "qtd_itens": int(qtd_raw) if qtd_raw.isdigit() else None,
                "observacao": "",
                "concluida": False,
            }
        )
    return registros


def parse_texto_bruto(texto: str):
    """Dispatcher usado no campo 'Importar novas tasks': primeiro tenta
    ler como tabela simples (Status/Task, separada por tab — com ou sem
    as demais colunas); se o texto não estiver nesse formato, cai para o
    parser por regex original (texto corrido copiado do sistema)."""
    registros = parse_status_task_simples(texto)
    if registros is not None:
        return registros
    return parse_texto(texto)


def parse_tabela(texto: str):
    """Lê o formato tabular colado direto da planilha/relatório, com
    cabeçalho: Status, Task, Priority, Source location, No. Of task
    details, Transaction (separados por tab). Retorna uma lista de dicts
    já no formato usado pela tabela `tasks`."""
    texto_limpo = texto.strip().strip('"')
    df_colado = pd.read_csv(
        io.StringIO(texto_limpo), sep="\t", dtype=str, keep_default_na=False
    )
    df_colado = _normalizar_colunas(df_colado)

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


def mover_para_historico(rows, usuario_email):
    """Move uma lista de rows (dicts com 'id') da tabela `tasks` para
    `historico_tasks` em lote: insere tudo no histórico primeiro e só
    depois apaga da tabela principal. `usuario_email` é gravado em
    `concluido_por` para saber quem marcou cada task como concluída.

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
        item["concluido_por"] = usuario_email
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
email_usuario_atual = st.session_state["user"].email
is_admin = email_usuario_atual.lower() in ADMIN_EMAILS

st.sidebar.write(f"👤 {email_usuario_atual}" + (" · 🛠️ admin" if is_admin else ""))
if st.sidebar.button("Sair"):
    logout()

st.title("📦 Gestão de Tasks por Andar")

# --- Importar novas tasks (texto corrido, com form que limpa sozinho) ---
with st.expander("➕ Importar novas tasks (colar texto bruto)", expanded=False):
    st.caption(
        "Aceita tanto o texto corrido copiado do sistema quanto uma tabela "
        "simples separada por tab com pelo menos as colunas Status e Task "
        "(sem precisar de Source location/Transaction)."
    )
    with st.form("form_texto_bruto", clear_on_submit=True):
        texto_bruto = st.text_area("Cole aqui o texto copiado do sistema", height=200)
        enviar_texto = st.form_submit_button("Processar e salvar")

    if enviar_texto:
        if not texto_bruto.strip():
            st.warning("Cole algum conteúdo antes de processar.")
        else:
            registros = parse_texto_bruto(texto_bruto)
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

# --- Menu lateral: navegar entre Andares, Histórico, Chat e (se admin) Admin ---
st.sidebar.divider()
opcoes_nav = ["📦 Andares", "🗂️ Histórico", "💬 Chat"]
if is_admin:
    opcoes_nav.append("🛠️ Admin")
pagina = st.sidebar.radio("📌 Navegação", opcoes_nav)

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
                            mover_para_historico(rows_para_mover, email_usuario_atual)
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
                        mover_para_historico(selecionados, email_usuario_atual)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Não foi possível concluir a(s) task(s): {e}")

                st.divider()

elif pagina == "🗂️ Histórico":
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

        # --- Filtros: busca por task, Andar, Transaction e Concluído por ---
        col_busca, col_andar, col_transacao, col_concluido_por = st.columns(
            [2, 1, 1, 1.3]
        )
        with col_busca:
            busca = st.text_input("🔎 Buscar por task", key="busca_historico")
        with col_andar:
            opcoes_andar = ["Todos"] + sorted(
                df_hist["andar"].dropna().unique().tolist()
            )
            filtro_andar = st.selectbox("🏢 Andar", opcoes_andar, key="filtro_andar_historico")
        with col_transacao:
            opcoes_transacao = ["Todas"] + sorted(
                df_hist["transaction"].dropna().unique().tolist()
            )
            filtro_transacao = st.selectbox(
                "🔁 Transaction", opcoes_transacao, key="filtro_transacao_historico"
            )
        with col_concluido_por:
            if "concluido_por" in df_hist.columns:
                opcoes_concluido_por = ["Todos"] + sorted(
                    df_hist["concluido_por"].dropna().unique().tolist()
                )
            else:
                opcoes_concluido_por = ["Todos"]
            filtro_concluido_por = st.selectbox(
                "👤 Concluído por",
                opcoes_concluido_por,
                key="filtro_concluido_por_historico",
            )

        if busca:
            df_hist = df_hist[df_hist["task"].str.contains(busca, case=False, na=False)]
        if filtro_andar != "Todos":
            df_hist = df_hist[df_hist["andar"] == filtro_andar]
        if filtro_transacao != "Todas":
            df_hist = df_hist[df_hist["transaction"] == filtro_transacao]
        if filtro_concluido_por != "Todos" and "concluido_por" in df_hist.columns:
            df_hist = df_hist[df_hist["concluido_por"] == filtro_concluido_por]

        colunas_exibir = [
            "task",
            "transaction",
            "source_location",
            "andar",
            "priority",
            "qtd_itens",
            "observacao",
            "concluida",
            "concluido_por",
            "data_evento",
        ]
        colunas_exibir = [c for c in colunas_exibir if c in df_hist.columns]

        if df_hist.empty:
            st.info("Nenhum registro encontrado para esse filtro.")
        else:
            # "dia" já sai em ordem decrescente por causa do sort acima
            dias = df_hist["dia"].drop_duplicates().tolist()
            for dia in dias:
                df_dia = df_hist[df_hist["dia"] == dia]
                with st.expander(f"📅 {dia} — {len(df_dia)} registro(s)", expanded=False):
                    st.dataframe(
                        df_dia[colunas_exibir],
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "concluido_por": st.column_config.TextColumn(
                                "Concluído por"
                            ),
                            "data_evento": st.column_config.DatetimeColumn(
                                "Data/Hora", format="DD/MM/YYYY HH:mm"
                            ),
                        },
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

elif pagina == "💬 Chat":
    # --- Página Chat: mensagens gerais do time, visíveis para todos os
    #     usuários logados. O bloco roda dentro de um st.fragment com
    #     run_every="3s": só ESSE pedaço da tela é recarregado a cada 3
    #     segundos (não o app inteiro), então mensagens de outras pessoas
    #     — e quem está online — aparecem sozinhas sem atrapalhar o que
    #     você estiver fazendo em outras abas/telas.
    st.title("💬 Chat do Time")

    # --- Visual estilo Discord: fundo escuro, avatares redondos com
    #     iniciais coloridas, mensagens agrupadas por autor, e os botões
    #     de editar/apagar só aparecem ao passar o mouse em cima da
    #     mensagem (usando o truque de dar `key=` ao st.container, que o
    #     Streamlit transforma numa classe CSS `st-key-<key>` — daí dá
    #     pra usar `:hover` num container pra revelar botões de outro). ---
    st.markdown(
        """
        <style>
        div[class*="st-key-discord_area"] {
            background-color: #313338;
            border-radius: 12px;
            padding: 4px 8px !important;
        }
        div[class*="st-key-msg_row_"] {
            border-radius: 6px;
            padding: 2px 8px;
            transition: background-color 0.1s;
        }
        div[class*="st-key-msg_row_"]:hover {
            background-color: #2b2d31;
        }
        div[class*="st-key-msg_actions_"] {
            opacity: 0;
            transition: opacity 0.1s;
        }
        div[class*="st-key-msg_row_"]:hover div[class*="st-key-msg_actions_"] {
            opacity: 1;
        }
        div[class*="st-key-msg_actions_"] button {
            padding: 0 0.35rem !important;
            font-size: 0.65rem !important;
            min-height: 1.3rem !important;
            height: 1.3rem !important;
            line-height: 1 !important;
            background-color: #2b2d31 !important;
            border: none !important;
        }
        div[class*="st-key-discord_online"] {
            background-color: #2b2d31;
            border-radius: 8px;
            padding: 6px 10px !important;
        }
        [data-testid="stChatInput"] {
            border-radius: 20px !important;
            background-color: #383a40 !important;
        }
        [data-testid="stChatInput"] textarea {
            color: #dbdee1 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    PALETA_AVATAR = [
        "#5865F2", "#57F287", "#FEE75C", "#EB459E",
        "#ED4245", "#3BA55D", "#FAA61A", "#00AFF4",
    ]

    def _cor_usuario(email: str) -> str:
        return PALETA_AVATAR[hash(email) % len(PALETA_AVATAR)]

    def _iniciais_usuario(email: str) -> str:
        nome = email.split("@")[0]
        return (nome[:2] or "??").upper()

    def _avatar_html(email: str, tamanho: int = 36) -> str:
        cor = _cor_usuario(email)
        iniciais = _iniciais_usuario(email)
        return (
            f'<div style="width:{tamanho}px;height:{tamanho}px;border-radius:50%;'
            f'background-color:{cor};display:flex;align-items:center;'
            f'justify-content:center;color:#fff;font-weight:600;'
            f'font-size:{tamanho * 0.4}px;flex-shrink:0;">{iniciais}</div>'
        )

    def _formatar_hora_discord(dt: pd.Timestamp) -> str:
        agora = pd.Timestamp.now(tz=dt.tz) if dt.tzinfo else pd.Timestamp.now()
        if dt.date() == agora.date():
            return f"Hoje às {dt.strftime('%H:%M')}"
        return dt.strftime("%d/%m/%Y %H:%M")

    JANELA_ONLINE_SEGUNDOS = 10

    @st.fragment(run_every="3s")
    def chat_fragment():
        email_atual = st.session_state["user"].email

        # --- Marca presença do usuário atual e busca quem mais está online ---
        try:
            supabase.table("presenca_usuarios").upsert(
                {"email": email_atual, "ultimo_acesso": datetime.now().isoformat()}
            ).execute()
            cutoff = (
                datetime.now() - timedelta(seconds=JANELA_ONLINE_SEGUNDOS)
            ).isoformat()
            online_dados = (
                supabase.table("presenca_usuarios")
                .select("email")
                .gte("ultimo_acesso", cutoff)
                .execute()
                .data
            )
            online_emails = sorted({o["email"] for o in online_dados})
        except Exception:
            online_emails = [email_atual]

        # --- Barra "quem está online" com bolinha verde + mini avatares,
        #     parecido com a lista de membros online do Discord ---
        with st.container(key="discord_online"):
            chips = "".join(
                f'<span style="display:inline-flex;align-items:center;gap:6px;'
                f'margin-right:14px;">'
                f'{_avatar_html(e, 20)}'
                f'<span style="color:#dbdee1;font-size:0.85rem;">{e}</span>'
                f'<span style="width:8px;height:8px;border-radius:50%;'
                f'background-color:#23a55a;display:inline-block;"></span>'
                f'</span>'
                for e in online_emails
            )
            st.markdown(
                f'<div style="color:#949ba4;font-size:0.75rem;margin-bottom:4px;">'
                f'🟢 ONLINE — {len(online_emails)}</div>'
                f'<div>{chips or "—"}</div>',
                unsafe_allow_html=True,
            )

        mensagens = (
            supabase.table("mensagens_chat")
            .select("*")
            .order("criado_em", desc=False)
            .limit(200)
            .execute()
            .data
        )

        area_chat = st.container(height=450, key="discord_area")
        with area_chat:
            if not mensagens:
                st.caption("Nenhuma mensagem ainda. Seja o primeiro a escrever!")

            autor_anterior = None
            hora_anterior = None

            for msg in mensagens:
                autor = msg["usuario_email"]
                msg_id = msg["id"]
                hora_dt = pd.to_datetime(msg["criado_em"])
                hora_txt = _formatar_hora_discord(hora_dt)
                editando_key = f"editando_msg_{msg_id}"

                # Agrupa com a mensagem anterior se for do mesmo autor e
                # dentro de uma janela curta (estilo Discord: some com o
                # avatar/nome repetido em sequências da mesma pessoa).
                agrupada = (
                    autor == autor_anterior
                    and hora_anterior is not None
                    and (hora_dt - hora_anterior) < pd.Timedelta(minutes=5)
                    and not st.session_state.get(editando_key)
                )

                with st.container(key=f"msg_row_{msg_id}"):
                    if st.session_state.get(editando_key):
                        # --- Modo edição: campo de texto + salvar/cancelar ---
                        col_av, col_conteudo = st.columns([0.06, 0.94])
                        with col_av:
                            st.markdown(_avatar_html(autor), unsafe_allow_html=True)
                        with col_conteudo:
                            st.markdown(
                                f'<span style="color:{_cor_usuario(autor)};'
                                f'font-weight:600;">{autor}</span> '
                                f'<span style="color:#949ba4;font-size:0.75rem;">'
                                f'{hora_txt}</span>',
                                unsafe_allow_html=True,
                            )
                            texto_editado = st.text_input(
                                "Editar mensagem",
                                value=msg["mensagem"],
                                key=f"input_edicao_{msg_id}",
                                label_visibility="collapsed",
                            )
                            col_salvar, col_cancelar = st.columns([1, 1])
                            with col_salvar:
                                if st.button("💾 Salvar", key=f"salvar_{msg_id}"):
                                    supabase.table("mensagens_chat").update(
                                        {"mensagem": texto_editado, "editado": True}
                                    ).eq("id", msg_id).execute()
                                    st.session_state[editando_key] = False
                                    st.rerun(scope="fragment")
                            with col_cancelar:
                                if st.button("✖️ Cancelar", key=f"cancelar_{msg_id}"):
                                    st.session_state[editando_key] = False
                                    st.rerun(scope="fragment")
                    else:
                        sufixo_editado = (
                            ' <span style="color:#949ba4;font-size:0.7rem;">'
                            "(editado)</span>"
                            if msg.get("editado")
                            else ""
                        )
                        col_av, col_conteudo, col_acoes = st.columns(
                            [0.06, 0.82, 0.12]
                        )

                        with col_av:
                            if not agrupada:
                                st.markdown(_avatar_html(autor), unsafe_allow_html=True)

                        with col_conteudo:
                            if not agrupada:
                                st.markdown(
                                    f'<span style="color:{_cor_usuario(autor)};'
                                    f'font-weight:600;">{autor}</span> '
                                    f'<span style="color:#949ba4;font-size:0.75rem;">'
                                    f"{hora_txt}</span>",
                                    unsafe_allow_html=True,
                                )
                                st.markdown(
                                    f'<span style="color:#dbdee1;">'
                                    f"{msg['mensagem']}{sufixo_editado}</span>",
                                    unsafe_allow_html=True,
                                )
                            else:
                                # Mensagem agrupada: só o texto, com a hora
                                # discretinha aparecendo à esquerda ao passar
                                # o mouse (igual ao Discord).
                                st.markdown(
                                    f'<span style="color:#949ba4;font-size:0.65rem;'
                                    f'margin-right:6px;">{hora_dt.strftime("%H:%M")}'
                                    f'</span><span style="color:#dbdee1;">'
                                    f"{msg['mensagem']}{sufixo_editado}</span>",
                                    unsafe_allow_html=True,
                                )

                        with col_acoes:
                            if autor == email_atual:
                                with st.container(key=f"msg_actions_{msg_id}"):
                                    col_editar, col_apagar = st.columns([1, 1])
                                    with col_editar:
                                        if st.button(
                                            "✏️",
                                            key=f"editar_{msg_id}",
                                            help="Editar mensagem",
                                        ):
                                            st.session_state[editando_key] = True
                                            st.rerun(scope="fragment")
                                    with col_apagar:
                                        if st.button(
                                            "🗑️",
                                            key=f"apagar_{msg_id}",
                                            help="Apagar mensagem",
                                        ):
                                            supabase.table("mensagens_chat").delete().eq(
                                                "id", msg_id
                                            ).execute()
                                            st.rerun(scope="fragment")

                autor_anterior = autor
                hora_anterior = hora_dt

        nova_mensagem = st.chat_input("Digite sua mensagem para o time...")
        if nova_mensagem:
            supabase.table("mensagens_chat").insert(
                {"usuario_email": email_atual, "mensagem": nova_mensagem}
            ).execute()
            st.rerun(scope="fragment")

    chat_fragment()

else:
    # --- Página Admin (só visível/roteável para e-mails em ADMIN_EMAILS) ---
    st.title("🛠️ Administração de Contas")

    if not is_admin:
        # Defesa extra: mesmo que alguém force a URL/estado, sem estar na
        # lista ADMIN_EMAILS não vê nada aqui.
        st.error("Você não tem permissão para acessar esta página.")
        st.stop()

    supabase_admin = get_supabase_admin()
    if supabase_admin is None:
        st.warning(
            "Configure `SUPABASE_SERVICE_ROLE_KEY` no `secrets.toml` para "
            "habilitar a administração de contas (criar, trocar senha, "
            "ativar/desativar). Essa chave é diferente da anon key e deve "
            "ficar só no servidor — nunca a exponha publicamente. Você "
            "encontra ela em Project Settings > API > service_role."
        )
        st.stop()

    def _listar_usuarios():
        resposta = supabase_admin.auth.admin.list_users()
        # Versões recentes do supabase-py retornam a lista diretamente;
        # versões antigas retornam um objeto com atributo `.users`.
        lista = getattr(resposta, "users", resposta)
        return sorted(lista, key=lambda u: (u.email or "").lower())

    def _conta_desabilitada(usuario) -> bool:
        banido_ate = getattr(usuario, "banned_until", None)
        if not banido_ate:
            return False
        try:
            return pd.to_datetime(banido_ate, utc=True) > pd.Timestamp.now(tz="UTC")
        except Exception:
            return False

    try:
        usuarios = _listar_usuarios()
    except Exception as e:
        st.error(f"Não foi possível listar as contas: {e}")
        usuarios = []

    # --- Criar nova conta ---
    with st.expander("➕ Criar nova conta", expanded=False):
        with st.form("form_nova_conta", clear_on_submit=True):
            novo_email = st.text_input("Email")
            nova_senha = st.text_input("Senha", type="password")
            criar_conta = st.form_submit_button("Criar conta")

        if criar_conta:
            if not novo_email.strip() or not nova_senha:
                st.warning("Preencha email e senha.")
            elif len(nova_senha) < 6:
                st.warning("A senha precisa ter pelo menos 6 caracteres.")
            else:
                try:
                    supabase_admin.auth.admin.create_user(
                        {
                            "email": novo_email.strip(),
                            "password": nova_senha,
                            "email_confirm": True,
                        }
                    )
                    st.success(f"Conta {novo_email.strip()} criada com sucesso!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Não foi possível criar a conta: {e}")

    st.divider()
    st.subheader("👥 Contas existentes")

    if not usuarios:
        st.info("Nenhuma conta encontrada.")
    else:
        for usuario in usuarios:
            desabilitada = _conta_desabilitada(usuario)
            criado_em = ""
            if getattr(usuario, "created_at", None):
                try:
                    criado_em = pd.to_datetime(usuario.created_at).strftime("%d/%m/%Y")
                except Exception:
                    criado_em = ""

            col_email, col_status, col_senha, col_toggle = st.columns([3, 1.3, 1, 1.3])

            with col_email:
                sufixo_voce = " (você)" if usuario.email == email_usuario_atual else ""
                st.write(f"**{usuario.email}**{sufixo_voce}")
                if criado_em:
                    st.caption(f"Criada em {criado_em}")

            with col_status:
                if desabilitada:
                    st.markdown("🚫 Desabilitada")
                else:
                    st.markdown("✅ Ativa")

            with col_senha:
                with st.popover("🔑 Senha"):
                    with st.form(f"form_senha_{usuario.id}"):
                        nova_senha_admin = st.text_input(
                            "Nova senha", type="password", key=f"nova_senha_{usuario.id}"
                        )
                        confirmar_senha = st.form_submit_button("Salvar nova senha")
                    if confirmar_senha:
                        if len(nova_senha_admin) < 6:
                            st.warning("A senha precisa ter pelo menos 6 caracteres.")
                        else:
                            try:
                                supabase_admin.auth.admin.update_user_by_id(
                                    usuario.id, {"password": nova_senha_admin}
                                )
                                st.success("Senha atualizada!")
                            except Exception as e:
                                st.error(f"Não foi possível trocar a senha: {e}")

            with col_toggle:
                eh_voce_mesmo = usuario.email == email_usuario_atual
                rotulo_toggle = "✅ Ativar" if desabilitada else "🚫 Desativar"
                if st.button(
                    rotulo_toggle,
                    key=f"toggle_{usuario.id}",
                    disabled=eh_voce_mesmo,
                    help="Você não pode desativar a própria conta."
                    if eh_voce_mesmo
                    else None,
                ):
                    try:
                        nova_duracao = "none" if desabilitada else "876000h"
                        supabase_admin.auth.admin.update_user_by_id(
                            usuario.id, {"ban_duration": nova_duracao}
                        )
                        st.rerun()
                    except Exception as e:
                        st.error(f"Não foi possível atualizar a conta: {e}")

            st.divider()

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
