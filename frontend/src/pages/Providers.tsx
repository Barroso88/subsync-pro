import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ExternalLink, KeyRound, Loader2, PlugZap, XCircle } from "lucide-react";
import { api } from "../api";
import { fmtDate } from "../lib";
import { Badge, ErrorBox, Field, PageHeader, Skeleton, Toggle, useToast } from "../components/ui";
import { useSettings } from "../hooks";

interface P {
  name: string; title: string; homepage: string; supports_hash: boolean; enabled: boolean; configured: boolean;
  config: Record<string, any>; secrets: Record<string, { set: boolean; source: string }>; downloads_today: number; daily_limit: number | null;
  remaining_quota: number | null; quota_reset: string | null; last_ok_at: string | null; last_error: string | null; auth_ok: boolean | null;
}
const SECRET_LABEL: Record<string, string> = { opensubtitles_api_key: "API key", opensubtitles_username: "Utilizador", opensubtitles_password: "Palavra-passe", subdl_api_key: "API key" };

export default function Providers() {
  const q = useQuery({ queryKey: ["providers"], queryFn: () => api.get<P[]>("/providers") });
  return (
    <>
      <PageHeader title="Fornecedores" subtitle="Fontes de legendas. Apenas APIs oficiais, sem scraping nem contornar limites." />
      {q.isLoading && <Skeleton className="h-64" />}
      {q.isError && <ErrorBox error={q.error} retry={() => q.refetch()} />}
      <div className="space-y-4">{q.data?.map((p) => <ProviderCard key={p.name} p={p} />)}</div>
      <TranslationCard />
      <div className="card p-4 mt-4 text-sm text-mute flex gap-3"><PlugZap size={18} className="shrink-0 text-dim mt-0.5" /><div>A arquitetura usa <i>adapters</i>: novos fornecedores implementam a interface <code className="text-fg">Provider</code> em <code className="text-fg">backend/app/providers</code> e ficam registados em <code className="text-fg">registry.py</code>.</div></div>
    </>
  );
}

function ProviderCard({ p }: { p: P }) {
  const qc = useQueryClient();
  const toast = useToast();
  const settings = useSettings();
  const [vals, setVals] = useState<Record<string, string>>({});
  const [cfg, setCfg] = useState<Record<string, any>>(p.config);
  const [test, setTest] = useState<{ ok: boolean; message: string } | null>(null);
  const save = useMutation({
    mutationFn: async () => {
      const secrets = Object.fromEntries(Object.entries(vals).filter(([, v]) => v.trim()));
      if (Object.keys(secrets).length) await api.put("/secrets", { values: secrets });
      await api.put("/settings", { providers: { ...(settings.data?.providers ?? {}), [p.name]: cfg } });
    },
    onSuccess: () => { toast("ok", "Guardado"); setVals({}); qc.invalidateQueries({ queryKey: ["providers"] }); qc.invalidateQueries({ queryKey: ["settings"] }); },
    onError: (e: Error) => toast("bad", e.message),
  });
  const testM = useMutation({ mutationFn: () => api.post<{ ok: boolean; message: string }>(`/providers/${p.name}/test`), onSuccess: (r) => { setTest(r); qc.invalidateQueries({ queryKey: ["providers"] }); } });
  const num = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) => setCfg({ ...cfg, [k]: Number(e.target.value) });
  return (
    <div className="card p-5 animate-rise">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <div className="flex items-center gap-2.5 flex-wrap"><h2 className="font-semibold">{p.title}</h2>
            <Badge tone={!p.configured ? "warn" : p.auth_ok === false ? "bad" : "ok"}>{!p.configured ? "Sem credenciais" : p.auth_ok === false ? "Erro de autenticação" : "Configurado"}</Badge>
            {p.supports_hash && <Badge tone="info">Pesquisa por hash</Badge>}
          </div>
          <a className="text-xs text-info hover:underline inline-flex items-center gap-1 mt-1" href={p.homepage} target="_blank" rel="noreferrer noopener">{p.homepage}<ExternalLink size={11} /></a>
        </div>
        <Toggle checked={!!cfg.enabled} onChange={(v) => setCfg({ ...cfg, enabled: v })} label="Ativo" />
      </div>
      <div className="grid sm:grid-cols-3 gap-3 mt-5">
        {Object.keys(p.secrets).map((k) => (
          <Field key={k} label={SECRET_LABEL[k] ?? k} hint={p.secrets[k].set ? `Definido (${p.secrets[k].source === "env" ? "variável de ambiente" : "guardado"})` : "Não definido"}>
            <div className="relative"><KeyRound size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-dim" />
              <input className="input pl-9" type="password" autoComplete="off" disabled={p.secrets[k].source === "env"}
                placeholder={p.secrets[k].set ? "••••••••  (deixe vazio para manter)" : ""} value={vals[k] ?? ""} onChange={(e) => setVals({ ...vals, [k]: e.target.value })} /></div>
          </Field>
        ))}
      </div>
      {p.name === "subdl" && <div className="text-xs text-warn mt-2">Adapter escrito sem acesso à documentação oficial e testado só com respostas simuladas: use «Testar ligação» para validar. O SubDL não suporta pesquisa por hash; a variante «PT» não indica se é Portugal ou Brasil (é estimada pelo conteúdo depois de descarregar).</div>}
      {p.name === "opensubtitles" && <div className="text-xs text-dim mt-2">A pesquisa só precisa da API key. Para <b>descarregar</b>, o OpenSubtitles exige também utilizador e palavra-passe. Os segredos nunca são mostrados nem registados nos logs.</div>}
      <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3 mt-5">
        <Field label="Prioridade"><input className="input" type="number" min={1} value={cfg.priority ?? 1} onChange={num("priority")} /></Field>
        <Field label="Máx. resultados"><input className="input" type="number" min={1} max={100} value={cfg.max_results ?? 30} onChange={num("max_results")} /></Field>
        <Field label="Pedidos / minuto"><input className="input" type="number" min={1} max={120} value={cfg.requests_per_minute ?? 30} onChange={num("requests_per_minute")} /></Field>
        <Field label="Downloads / dia" hint="O fornecedor pode impor um limite inferior."><input className="input" type="number" min={1} value={cfg.daily_download_limit ?? 20} onChange={num("daily_download_limit")} /></Field>
      </div>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 mt-5 text-xs text-mute border-t border-line pt-4">
        <span>Downloads hoje: <b className="text-fg tabular-nums">{p.downloads_today}{p.daily_limit ? ` / ${p.daily_limit}` : ""}</b></span>
        {p.remaining_quota != null && <span>Quota do fornecedor restante: <b className="text-fg tabular-nums">{p.remaining_quota}</b></span>}
        {p.last_ok_at && <span>Último sucesso: {fmtDate(p.last_ok_at)}</span>}
        {p.last_error && <span className="text-bad">Último erro: {p.last_error}</span>}
      </div>
      {test && <div className={`mt-3 flex gap-2 text-sm ${test.ok ? "text-ok" : "text-bad"}`}>{test.ok ? <CheckCircle2 size={16} className="mt-0.5" /> : <XCircle size={16} className="mt-0.5" />}{test.message}</div>}
      <div className="flex gap-2 justify-end mt-4">
        <button className="btn-ghost" onClick={() => testM.mutate()} disabled={testM.isPending}>{testM.isPending && <Loader2 size={14} className="animate-spin" />}Testar ligação</button>
        <button className="btn-primary" onClick={() => save.mutate()} disabled={save.isPending}>Guardar</button>
      </div>
    </div>
  );
}

interface Tr { enabled: boolean; configured: boolean; engine: "deepl" | "gemini"; model: string | null; deepl_configured: boolean; gemini_configured: boolean; source: string; plan: string | null; usage: { used: number; limit: number; remaining: number } | null; error: string | null }

interface GKey { slot: number; name: string; set: boolean; source: string; exhausted_until: string | null }

/** Até 3 chaves Gemini (por ex. de contas diferentes do próprio utilizador): usa a primeira com quota e passa à seguinte quando a diária esgota. */
function GeminiKeys({ t, onSaved, onTest, testing }: { t?: Tr & { gemini_keys?: GKey[] }; onSaved: () => void; onTest: () => void; testing: boolean }) {
  const toast = useToast();
  const [vals, setVals] = useState<Record<string, string>>({});
  const save = useMutation({
    mutationFn: (name: string) => api.put("/secrets", { values: { [name]: (vals[name] ?? "").trim() } }),
    onSuccess: (_, name) => { toast("ok", "Chave guardada"); setVals((v) => ({ ...v, [name]: "" })); onSaved(); },
    onError: (e: Error) => toast("bad", e.message),
  });
  const keys: GKey[] = t?.gemini_keys ?? [1, 2, 3].map((i) => ({ slot: i, name: i === 1 ? "gemini_api_key" : `gemini_api_key_${i}`, set: false, source: "none", exhausted_until: null }));
  const shown = keys.filter((k) => k.slot === 1 || k.set || k.slot === (keys.filter((x) => x.set).length + 1));
  return (
    <div className="space-y-3">
      <div className="label">Chaves Gemini (aistudio.google.com/apikey)</div>
      {shown.map((k) => (
        <div key={k.name} className="space-y-1">
          <div className="flex gap-2"><div className="relative flex-1"><KeyRound size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-dim" />
            <input className="input pl-9" type="password" autoComplete="off" disabled={k.source === "env"} value={vals[k.name] ?? ""} onChange={(e) => setVals((v) => ({ ...v, [k.name]: e.target.value }))}
              placeholder={k.set ? "•••••••• (vazio = manter)" : k.slot === 1 ? "Chave principal" : `Chave ${k.slot} (opcional, outra conta)`} /></div>
            <button className="btn-primary" disabled={!(vals[k.name] ?? "").trim() || save.isPending} onClick={() => save.mutate(k.name)}>Guardar</button>
            {k.slot === 1 && <button className="btn-ghost" disabled={testing || !t?.configured} onClick={onTest}>{testing && <Loader2 size={14} className="animate-spin" />}Testar</button>}</div>
          <div className="flex gap-2 items-center text-xs text-mute flex-wrap">
            <span>Chave {k.slot}: {k.set ? `definida (${k.source === "env" ? "variável de ambiente" : "guardada"})` : "sem chave"}</span>
            {k.exhausted_until && <Badge tone="warn">quota diária esgotada até {new Date(k.exhausted_until).toLocaleString("pt-PT", { hour: "2-digit", minute: "2-digit" })}</Badge>}
          </div>
        </div>
      ))}
      <div className="text-xs text-dim">Com mais de uma chave, a app usa a primeira e só passa à seguinte quando o limite <b>diário</b> da anterior esgota (a tradução continua sem falhar). Cada chave tem a sua quota (por projeto/conta). Use apenas chaves das suas próprias contas e confirme os termos do Google.</div>
    </div>
  );
}

function TranslationCard() {
  const qc = useQueryClient();
  const toast = useToast();
  const settings = useSettings();
  const [key, setKey] = useState("");
  const [model, setModel] = useState<string | null>(null);
  const [modelList, setModelList] = useState<string[] | null>(null);
  const [testMsg, setTestMsg] = useState<{ ok: boolean; text: string; pairs?: { src: string; out: string }[] } | null>(null);
  const q = useQuery({ queryKey: ["translation"], queryFn: () => api.get<Tr>("/translation") });
  const t = q.data;
  const engine = t?.engine ?? "deepl";
  const cfg = settings.data?.translation ?? { enabled: false, auto_fallback: false, source_languages: ["en"] };
  const saveCfg = useMutation({
    mutationFn: (patch: Record<string, unknown>) => api.put("/settings", { translation: { ...cfg, ...patch } }),
    onSuccess: () => { setTestMsg(null); setKey(""); qc.invalidateQueries({ queryKey: ["settings"] }); qc.invalidateQueries({ queryKey: ["translation"] }); },
    onError: (e: Error) => toast("bad", e.message),
  });
  const save = useMutation({
    mutationFn: () => api.put("/secrets", { values: { [engine === "gemini" ? "gemini_api_key" : "deepl_api_key"]: key.trim() } }),
    onSuccess: () => { toast("ok", `Chave ${engine === "gemini" ? "Gemini" : "DeepL"} guardada`); setKey(""); qc.invalidateQueries({ queryKey: ["translation"] }); },
    onError: (e: Error) => toast("bad", e.message),
  });
  const test = useMutation({
    mutationFn: () => api.post<{ sample: string; model?: string; stats?: { requests: number; rate_limited: number; invalid: number; slowest: number }; pairs?: { src: string; out: string }[] }>("/translation/test"),
    onSuccess: (r) => setTestMsg({ ok: true, text: `Funciona${r.model ? ` (modelo ${r.model})` : ""}. Reveja a qualidade em pt-PT.${r.stats ? ` (${r.stats.requests} pedidos, o mais lento ${Math.round(r.stats.slowest)} s${r.stats.rate_limited ? `, ${r.stats.rate_limited} limitados (429)` : ""}${r.stats.invalid ? `, ${r.stats.invalid} respostas inválidas` : ""})` : ""}`, pairs: r.pairs }),
    onError: (e: Error) => setTestMsg({ ok: false, text: e.message }),
  });
  const models = useMutation({
    mutationFn: () => api.get<{ models: string[]; suggested: string | null }>("/translation/models"),
    onSuccess: (r) => setModelList(r.models), onError: (e: Error) => toast("bad", e.message),
  });
  const pct = t?.usage && t.usage.limit ? Math.min(100, (t.usage.used / t.usage.limit) * 100) : 0;
  const gem = engine === "gemini";
  return (
    <div className="card p-5 mt-4 animate-rise">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <div className="flex items-center gap-2.5 flex-wrap"><h2 className="font-semibold">Tradução automática · {gem ? "Gemini" : "DeepL"}</h2>
            <Badge tone={!t?.configured ? "warn" : t.error ? "bad" : "ok"}>{!t?.configured ? "Sem chave" : t.error ? "Erro" : gem ? `Chave definida (${t.model})` : `Ligado (${t.plan === "free" ? "plano gratuito" : "plano pago"})`}</Badge></div>
          <div className="text-xs text-mute mt-1 max-w-2xl">Traduz legendas (ex.: inglês → pt-PT/pt-BR) mantendo os tempos. O texto das legendas é enviado ao serviço escolhido. O resultado fica sempre assinalado como tradução automática e «por confirmar». Ative em Definições → Idiomas.</div>
        </div>
      </div>
      <div className="mt-4 max-w-xl space-y-4">
        <Field label="Motor de tradução" hint={gem ? "Modelo de IA: traduz com o contexto das falas vizinhas e respeita «português de Portugal». Pode recusar falas (violência/sexo): essas ficam no original e a tradução é rejeitada se forem mais de 3 %. O escalão gratuito pode usar os pedidos para melhorar produtos do Google." : "Tradução automática clássica, com quota mensal em caracteres."}>
          <select className="input" value={engine} disabled={saveCfg.isPending} onChange={(e) => saveCfg.mutate({ engine: e.target.value })}>
            <option value="deepl">DeepL</option><option value="gemini">Gemini (Google AI Studio)</option></select>
        </Field>
        {gem && (
          <Field label="Modelo Gemini" hint="Vazio = automático (recomendado): a app escolhe o «flash-lite» mais recente (limites gratuitos muito maiores) ou, se não houver, o Flash mais recente. Use «Listar modelos» para ver o que está disponível.">
            <div className="flex gap-2"><input className="input" placeholder="automático" value={model ?? cfg.gemini_model ?? ""} onChange={(e) => setModel(e.target.value)} />
              <button className="btn-ghost" disabled={model === null || saveCfg.isPending} onClick={() => saveCfg.mutate({ gemini_model: (model ?? "").trim() }, { onSuccess: () => setModel(null) })}>Guardar</button>
              <button className="btn-ghost" disabled={!t?.configured || models.isPending} onClick={() => models.mutate()}>{models.isPending && <Loader2 size={14} className="animate-spin" />}Listar modelos</button></div>
            {modelList && <div className="flex gap-1.5 flex-wrap mt-2">{modelList.filter((m) => m.includes("flash") || m.includes("gemma")).map((m) => <button key={m} className="h-6 px-2 rounded-md text-xs border border-line text-mute hover:text-fg" onClick={() => setModel(m)}>{m}</button>)}
              {modelList.length === 0 && <span className="text-xs text-dim">Nenhum modelo devolvido.</span>}</div>}
          </Field>
        )}
        {gem ? <GeminiKeys t={t} onSaved={() => qc.invalidateQueries({ queryKey: ["translation"] })} onTest={() => { setTestMsg(null); test.mutate(); }} testing={test.isPending} /> : (
          <Field label={gem ? "API key do Gemini" : "API key do DeepL"} hint={t?.configured ? `Definida (${t.source === "env" ? "variável de ambiente" : "guardada"}).${gem ? "" : " Chaves gratuitas terminam em :fx."}` : gem ? "Crie uma chave em aistudio.google.com/apikey." : "Crie uma chave em deepl.com/pro-api (plano API Free)."}>
            <div className="flex gap-2"><div className="relative flex-1"><KeyRound size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-dim" />
              <input className="input pl-9" type="password" autoComplete="off" disabled={t?.source === "env"} value={key} onChange={(e) => setKey(e.target.value)} placeholder={t?.configured ? "•••••••• (vazio = manter)" : ""} /></div>
              <button className="btn-primary" disabled={!key.trim() || save.isPending} onClick={() => save.mutate()}>Guardar</button>
              <button className="btn-ghost" disabled={q.isFetching || test.isPending || !t?.configured} onClick={() => { setTestMsg(null); if (gem) test.mutate(); else q.refetch(); }}>{(q.isFetching || test.isPending) && <Loader2 size={14} className="animate-spin" />}Testar</button></div>
          </Field>
        )}
      </div>
      {testMsg && <div className={`mt-3 text-sm flex gap-2 ${testMsg.ok ? "text-ok" : "text-bad"}`}>{testMsg.ok ? <CheckCircle2 size={16} className="mt-0.5 shrink-0" /> : <XCircle size={16} className="mt-0.5 shrink-0" />}<div>{testMsg.text}
        {testMsg.pairs && <div className="mt-2 space-y-1 text-xs text-fg">{testMsg.pairs.map((p, i) => <div key={i}><span className="text-mute">{p.src}</span> → <b>{p.out}</b></div>)}</div>}</div></div>}
      {t?.error && <div className="mt-3 text-sm text-bad flex gap-2"><XCircle size={16} className="mt-0.5" />{t.error}</div>}
      {t?.usage && (
        <div className="mt-4 max-w-xl">
          <div className="flex justify-between text-xs text-mute mb-1.5"><span>Caracteres usados este período</span><span className="tabular-nums">{t.usage.used.toLocaleString("pt-PT")} / {t.usage.limit.toLocaleString("pt-PT")}</span></div>
          <div className="h-2 rounded-full bg-s3 overflow-hidden"><div className={`h-full rounded-full ${pct > 90 ? "bg-bad" : "bg-accent"}`} style={{ width: `${pct}%` }} /></div>
          <div className="text-xs text-dim mt-1.5">Restam {t.usage.remaining.toLocaleString("pt-PT")} caracteres. Um filme usa tipicamente 40 000–60 000 (estimativa).</div>
        </div>
      )}
    </div>
  );
}
