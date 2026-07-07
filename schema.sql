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
  created_at timestamptz,
  turno_fechado_em timestamptz not null default now()
);

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

create policy "authenticated_full_access_tasks"
  on public.tasks for all
  to authenticated
  using (true)
  with check (true);

create policy "authenticated_full_access_historico"
  on public.historico_tasks for all
  to authenticated
  using (true)
  with check (true);
