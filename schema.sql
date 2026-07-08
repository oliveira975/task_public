-- ================================================================
-- Schema para o app de Gestão de Tasks por Andar (Streamlit + Supabase)
-- Rode este script inteiro no SQL Editor do seu projeto Supabase.
-- ================================================================

-- Tabela principal: tasks do turno ATUAL (equivalente à antiga aba Dados_Tratados)
create table if not exists public.tasks (
  id bigint generated always as identity primary key,
  status text not null,
  task text not null unique,
  source_location text not null,
  andar text not null,
  transaction text not null,
  priority integer,
  qtd_itens integer,
  concluida boolean not null default false,
  created_at timestamptz not null default now()
);

-- Histórico: snapshot de cada turno fechado (equivalente às cópias "Turno_Concluido_..." no Drive)
create table if not exists public.historico_tasks (
  id bigint generated always as identity primary key,
  status text,
  task text,
  source_location text,
  andar text,
  transaction text,
  priority integer,
  qtd_itens integer,
  concluida boolean,
  concluido_por text,
  created_at timestamptz,
  turno_fechado_em timestamptz not null default now()
);

-- Migração: se seu projeto já tinha rodado uma versão anterior deste
-- schema (sem a coluna concluido_por), rode esta linha para atualizar:
-- alter table public.historico_tasks add column if not exists concluido_por text;

-- Migração: se você já tinha rodado uma versão anterior deste schema
-- (sem priority/qtd_itens), rode só estas linhas para atualizar:
-- alter table public.tasks add column if not exists priority integer;
-- alter table public.tasks add column if not exists qtd_itens integer;
-- alter table public.historico_tasks add column if not exists priority integer;
-- alter table public.historico_tasks add column if not exists qtd_itens integer;

-- Índices úteis para os agrupamentos que o app faz o tempo todo
create index if not exists idx_tasks_andar on public.tasks (andar);
create index if not exists idx_tasks_transaction on public.tasks (transaction);
create index if not exists idx_historico_turno on public.historico_tasks (turno_fechado_em);

-- Row Level Security: só usuários autenticados (login email/senha) podem
-- ler/gravar. Como é uma ferramenta interna de equipe, qualquer usuário
-- autenticado tem acesso total às duas tabelas.
alter table public.tasks enable row level security;
alter table public.historico_tasks enable row level security;

drop policy if exists "authenticated_full_access_tasks" on public.tasks;
create policy "authenticated_full_access_tasks"
  on public.tasks for all
  to authenticated
  using (true)
  with check (true);

drop policy if exists "authenticated_full_access_historico" on public.historico_tasks;
create policy "authenticated_full_access_historico"
  on public.historico_tasks for all
  to authenticated
  using (true)
  with check (true);

-- ================================================================
-- Chat do time
-- ================================================================
create table if not exists public.mensagens_chat (
  id bigint generated always as identity primary key,
  usuario_email text not null,
  mensagem text not null,
  editado boolean not null default false,
  criado_em timestamptz not null default now()
);

create index if not exists idx_mensagens_chat_criado_em
  on public.mensagens_chat (criado_em);

alter table public.mensagens_chat enable row level security;

drop policy if exists "authenticated_full_access_mensagens_chat" on public.mensagens_chat;
create policy "authenticated_full_access_mensagens_chat"
  on public.mensagens_chat for all
  to authenticated
  using (true)
  with check (true);

-- ================================================================
-- Presença online (usado pelo Chat para mostrar quem está online)
-- Cada usuário tem uma única linha, atualizada a cada refresh do chat.
-- ================================================================
create table if not exists public.presenca_usuarios (
  email text primary key,
  ultimo_acesso timestamptz not null default now()
);

alter table public.presenca_usuarios enable row level security;

drop policy if exists "authenticated_full_access_presenca" on public.presenca_usuarios;
create policy "authenticated_full_access_presenca"
  on public.presenca_usuarios for all
  to authenticated
  using (true)
  with check (true);
