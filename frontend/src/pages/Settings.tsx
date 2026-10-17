import { ReactNode, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUp, Eye, FolderOpen, FolderPlus, Loader2, Lock, Plus, ScanSearch, ShieldCheck, Trash2, Unlock, X } from "lucide-react";
import { api, Library, Settings } from "../api";
import { fmtDate } from "../lib";
import { useMeta, useSettings, useStartJob } from "../hooks";
import { Badge, Empty, Field, Modal, PageHeader, Skeleton, Toggle, useToast } from "../components/ui";

function Section({ title, hint, children, actions }: { title: string; hint?: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="card p-5 animate-rise">
      <div className="flex items-start justify-between gap-3 mb-4"><div><h2 className="font-semibold">{title}</h2>{hint && <p className="text-xs text-mute mt-0.5 max-w-2xl">{hint}</p>}</div>{actions}</div>
      {children}
    </section>
  );
}

export default function SettingsPage() {
  const s = useSettings();
  const meta = useMeta();
  const qc = useQueryClient();
  const toast = useToast();
  const [d, setD] = useState<Settings | null>(null);
  useEffect(() => { if (s.data && !d) setD(s.data); }, [s.data]);
  const save = useMutation({
    mutationFn: () => api.put<Settings>("/settings", d),
    onSuccess: (r) => { setD(r); qc.invalidateQueries(); toast("ok", "Definições guardadas"); },
    onError: (e: Error) => toast("bad", e.message),
  });
  if (!d) return <div className="space-y-4"><Skeleton className="h-8 w-40" /><Skeleton className="h-56" /><Skeleton className="h-56" /></div>;
  const set = (k: string, v: any) => setD({ ...d, [k]: v });
  const dirty = JSON.stringify(d) !== JSON.stringify(s.data);
  const used = new Set(d.languages.map((l: any) => l.code));
  const allLangs = [...(meta.data?.languages ?? [])];
  return (
    <>
      <PageHeader title="Definições" subtitle="Bibliotecas, idiomas, comportamento dos downloads e integrações."
        actions={<><button className="btn-primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>{save.isPending && <Loader2 size={14} className="animate-spin" />}Guardar alterações</button></>} />
      <div className="space-y-5 max-w-4xl">
        <Libraries />

        <Section title="Idiomas" hint="Prioritários têm de existir (senão o item aparece como «em falta»). Secundários só entram na pesquisa. pt, pt-PT e pt-BR são tratados como idiomas distintos.">
          <div className="space-y-2">
            {d.languages.map((l: any, i: number) => (
              <div key={l.code} className="flex items-center gap-2.5 card bg-s2 px-3 py-2">
                <button className="btn-quiet !h-7 !w-7 !p-0" disabled={i === 0} aria-label="Subir" onClick={() => { const a = [...d.languages]; [a[i - 1], a[i]] = [a[i], a[i - 1]]; set("languages", a); }}><ArrowUp size={14} /></button>
                <span className="flex-1 text-sm">{allLangs.find((x) => x.code === l.code)?.name ?? l.code} <span className="text-dim">({l.code})</span></span>
                <select className="input !w-auto !h-8" value={l.priority} onChange={(e) => set("languages", d.languages.map((x: any) => x.code === l.code ? { ...x, priority: e.target.value } : x))}>
                  <option value="primary">Prioritário</option><option value="secondary">Secundário</option>
                </select>
                <button className="btn-quiet !h-7 !w-7 !p-0" aria-label="Remover" onClick={() => set("languages", d.languages.filter((x: any) => x.code !== l.code))}><X size={14} /></button>
              </div>
            ))}
            <select className="input" value="" onChange={(e) => e.target.value && set("languages", [...d.languages, { code: e.target.value, priority: "secondary" }])}>
              <option value="">+ Adicionar idioma…</option>
              {allLangs.filter((l) => !used.has(l.code)).map((l) => <option key={l.code} value={l.code}>{l.name} ({l.code})</option>)}
            </select>
          </div>
          <div className="grid sm:grid-cols-2 gap-4 mt-5">
            <Toggle checked={d.embedded_counts} onChange={(v) => set("embedded_counts", v)} label="Legendas embutidas contam para a cobertura" hint="Se desligado, só ficheiros externos satisfazem um idioma." />
            <Toggle checked={d.prefer_full} onChange={(v) => set("prefer_full", v)} label="Preferir legendas completas" hint="Forçadas nunca contam como cobertura do idioma." />
            <Field label="Legendas «pt» sem variante" hint="Nome/faixa só diz «pt» e o conteúdo não permite saber se é de Portugal ou do Brasil. Assumir uma variante evita ler os vídeos para a estimar. Fica assinalado (≈pt).">
              <select className="input" value={d.generic_pt_variant ?? ""} onChange={(e) => set("generic_pt_variant", e.target.value)}>
                <option value="">Não contar (ficam em falta até estimar)</option>
                <option value="pt-PT">Assumir pt-PT</option>
                <option value="pt-BR">Assumir pt-BR</option>
              </select></Field>
            <Toggle checked={!!d.accept_pt_br_fallback} onChange={(v) => set("accept_pt_br_fallback", v)} label="Aceitar pt-BR quando faltar pt-PT" hint="Pesquisa também pt-BR; uma legenda pt-BR conta como cobertura de pt-PT (assinalada) e é guardada como .pt-BR.srt, nunca renomeada. Em lote, pt-PT tem sempre prioridade." />
            <Toggle checked={!!d.translation?.enabled} onChange={(v) => set("translation", { ...d.translation, enabled: v })} label="Permitir tradução automática (DeepL)" hint="Mostra «Traduzir» nos resultados noutros idiomas. Requer chave DeepL em Fornecedores. O texto é enviado ao DeepL." />
            <Toggle checked={!!d.translation?.auto_fallback} disabled={!d.translation?.enabled} onChange={(v) => set("translation", { ...d.translation, auto_fallback: v })} label="Em lote, traduzir de inglês se não houver legenda" hint="Fica sempre «por confirmar»: nunca é instalada automaticamente. Consome caracteres do DeepL." />
            <Toggle checked={d.prefer_forced} onChange={(v) => set("prefer_forced", v)} label="Incluir legendas forçadas nas pesquisas" />
            <Field label="Legendas SDH/HI"><select className="input" value={d.hi_policy} onChange={(e) => set("hi_policy", e.target.value)}><option value="exclude">Excluir</option><option value="include">Incluir</option><option value="only">Apenas SDH/HI</option></select></Field>
          </div>
        </Section>

        <Section title="Downloads automáticos" hint="Por omissão nada é descarregado sem a sua confirmação. Mesmo em modo automático, ficheiros existentes nunca são substituídos.">
          <div className="grid sm:grid-cols-2 gap-4">
            <Toggle checked={d.auto_download} onChange={(v) => set("auto_download", v)} label="Procurar automaticamente após cada análise" />
            <Toggle checked={d.require_confirmation} onChange={(v) => set("require_confirmation", v)} label="Exigir confirmação manual antes de instalar" hint="Candidatas ficam «Por confirmar» no histórico." />
            <Field label="Nível mínimo de compatibilidade"><select className="input" value={d.auto_min_tier} onChange={(e) => set("auto_min_tier", e.target.value)}><option value="hash">Apenas confirmada por hash</option><option value="probable">Provável (metadados) ou melhor</option></select></Field>
            <Field label="Máx. itens por execução"><input className="input" type="number" min={1} max={500} value={d.auto_max_per_run} onChange={(e) => set("auto_max_per_run", Number(e.target.value))} /></Field>
            <Toggle checked={d.notify_plex_after_install} onChange={(v) => set("notify_plex_after_install", v)} label="Pedir ao Plex para atualizar o item após instalar" />
          </div>
        </Section>

        <Section title="Scanner" hint="Reanálise incremental: só ficheiros novos ou alterados são processados de novo."
          actions={<RevalidateButton />}>
          <div className="mb-5"><Toggle checked={!!d.embedded_variant_scan} onChange={(v) => set("embedded_variant_scan", v)} disabled={!!d.generic_pt_variant}
            label="Ler o texto das legendas embutidas «pt» para estimar pt-PT/pt-BR (LENTO)"
            hint={d.generic_pt_variant ? "Desativado enquanto «pt» sem variante estiver a ser assumido (Definições → Idiomas)." : "Abre e lê quase todo o vídeo (ffmpeg) em cada ficheiro afetado: intenso em disco/rede. Desligado por omissão; sem isto a app nunca lê o conteúdo dos vídeos para além do FFprobe."} /></div>
          <Field label="Intervalo da análise agendada (minutos)" hint="0 = apenas manual. O número máximo de tarefas simultâneas define-se por variável de ambiente (SUBSYNC_MAX_JOBS).">
            <input className="input max-w-[200px]" type="number" min={0} value={d.scan_interval_minutes} onChange={(e) => set("scan_interval_minutes", Number(e.target.value))} />
          </Field>
        </Section>

        {(["plex", "radarr", "sonarr"] as const).map((n) => <Integration key={n} name={n} d={d} set={set} />)}

        <Section title="Segurança">
          <div className="space-y-2 text-sm">
            <div className="flex items-center gap-2.5"><ShieldCheck size={16} className={meta.data?.auth_required ? "text-ok" : "text-warn"} />{meta.data?.auth_required ? "Autenticação por palavra-passe ativa." : <span className="text-warn">Sem autenticação: não exponha esta porta à Internet. Defina SUBSYNC_AUTH_PASSWORD.</span>}</div>
            <div className="text-mute text-xs">Raízes permitidas para bibliotecas: <code className="text-fg">{meta.data?.allowed_roots.join(", ")}</code></div>
            <div className="text-mute text-xs">Pastas com escrita autorizada agora: {meta.data?.writable_roots.length ? meta.data.writable_roots.map((r) => <Badge key={r} tone="warn" className="mr-1">{r}</Badge>) : "nenhuma (modo só de leitura)"}</div>
          </div>
        </Section>
      </div>
      {dirty && (
        <div className="fixed bottom-0 left-0 right-0 lg:left-[248px] z-30 border-t border-line bg-s1/95 backdrop-blur px-4 sm:px-8 py-3 flex items-center justify-between gap-3 animate-rise">
          <span className="text-sm text-mute">Tem alterações por guardar.</span>
          <div className="flex gap-2">
            <button className="btn-ghost" onClick={() => setD(s.data ?? null)} disabled={save.isPending}>Descartar</button>
            <button className="btn-primary" onClick={() => save.mutate()} disabled={save.isPending}>{save.isPending && <Loader2 size={14} className="animate-spin" />}Guardar alterações</button>
          </div>
        </div>
      )}
    </>
  );
}

function RevalidateButton() {
  const start = useStartJob();
  return <button className="btn-ghost" disabled={start.isPending} title="Relê todas as legendas externas (idioma, variante pt-PT/pt-BR, validade). Não volta a executar o FFprobe nos vídeos."
    onClick={() => start.mutate({ url: "/revalidate" })}><ScanSearch size={15} />Reclassificar legendas</button>;
}

function Libraries() {
  const qc = useQueryClient();
  const toast = useToast();
  const scan = useStartJob();
  const [add, setAdd] = useState(false);
  const libs = useQuery({ queryKey: ["libraries"], queryFn: () => api.get<Library[]>("/libraries") });
  const patch = useMutation({ mutationFn: ({ id, ...b }: { id: number; writable?: boolean }) => api.patch(`/libraries/${id}`, b), onSuccess: () => { qc.invalidateQueries(); }, onError: (e: Error) => toast("bad", e.message) });
  const del = useMutation({ mutationFn: (id: number) => api.del(`/libraries/${id}`), onSuccess: () => qc.invalidateQueries() });
  return (
    <Section title="Bibliotecas" hint="O scanner só lê vídeos. Escrever legendas exige ativar «escrita» e a pasta estar montada com permissões de escrita."
      actions={<button className="btn-primary" onClick={() => setAdd(true)}><FolderPlus size={15} />Adicionar</button>}>
      {libs.isLoading && <Skeleton className="h-20" />}
      {libs.data?.length === 0 && <Empty icon={<FolderOpen size={22} />} title="Nenhuma biblioteca" hint="Adicione as pastas de filmes e séries (têm de estar dentro das raízes permitidas)." />}
      <div className="space-y-2.5">
        {libs.data?.map((l) => (
          <div key={l.id} className="card bg-s2 p-3.5">
            <div className="flex items-start gap-3 flex-wrap">
              <div className="flex-1 min-w-[220px]">
                <div className="flex items-center gap-2 flex-wrap"><span className="font-medium text-sm">{l.name}</span><Badge>{l.type === "movies" ? "Filmes" : "Séries"}</Badge>
                  {l.effective_write ? <Badge tone="warn"><Unlock size={10} />Leitura + escrita de legendas</Badge> : <Badge tone="ok"><Lock size={10} />Só leitura</Badge>}
                  {!l.exists && <Badge tone="bad">Pasta inacessível</Badge>}{l.writable && !l.os_writable && l.exists && <Badge tone="bad" title="Sem permissão no sistema de ficheiros">Sem permissão de escrita</Badge>}</div>
                <div className="font-mono text-xs text-dim mt-1 break-all">{l.path}</div>
                <div className="text-xs text-dim mt-1">{l.counts.media} itens · última análise {fmtDate(l.last_scan_at)}</div>
              </div>
              <Toggle checked={!!l.writable} onChange={(v) => patch.mutate({ id: l.id, writable: v })} label="Permitir escrita de legendas" />
              <div className="flex gap-1.5">
                <button className="btn-ghost !h-8" onClick={() => scan.mutate({ url: "/scan", body: { library_id: l.id } })}><ScanSearch size={14} />Analisar</button>
                <button className="btn-quiet !h-8 hover:!text-bad" aria-label="Remover" onClick={() => confirm(`Remover «${l.name}» da aplicação? Nenhum ficheiro no disco será apagado.`) && del.mutate(l.id)}><Trash2 size={14} /></button>
              </div>
            </div>
          </div>
        ))}
      </div>
      <AddLibrary open={add} onClose={() => setAdd(false)} />
    </Section>
  );
}

function AddLibrary({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [type, setType] = useState<"movies" | "series">("movies");
  const [writable, setWritable] = useState(false);
  const br = useQuery({ queryKey: ["browse", path], queryFn: () => api.get<{ path: string; dirs: string[]; writable?: boolean; roots: boolean }>(`/browse?path=${encodeURIComponent(path)}`), enabled: open });
  const create = useMutation({ mutationFn: () => api.post("/libraries", { name: name || path.split("/").pop(), type, path, writable }), onSuccess: () => { qc.invalidateQueries(); toast("ok", "Biblioteca adicionada"); onClose(); setPath(""); setName(""); }, onError: (e: Error) => toast("bad", e.message) });
  const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  return (
    <Modal open={open} onClose={onClose} title="Adicionar biblioteca" wide
      footer={<><button className="btn-ghost" onClick={onClose}>Cancelar</button><button className="btn-primary" disabled={!path || create.isPending} onClick={() => create.mutate()}>Adicionar</button></>}>
      <div className="space-y-4">
        <div className="grid sm:grid-cols-2 gap-3">
          <Field label="Nome"><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Ex.: Filmes" /></Field>
          <Field label="Tipo"><select className="input" value={type} onChange={(e) => setType(e.target.value as any)}><option value="movies">Filmes</option><option value="series">Séries</option></select></Field>
        </div>
        <Field label="Pasta" hint="Navegue até à pasta pretendida e clique em «Usar esta pasta».">
          <div className="card bg-s2 overflow-hidden">
            <div className="flex items-center gap-2 px-3 py-2 border-b border-line text-xs font-mono break-all">{path || "(raízes permitidas)"}</div>
            <div className="max-h-56 overflow-y-auto">
              {path && !br.data?.roots && <button className="w-full text-left px-3 py-2 text-sm hover:bg-s3 text-mute" onClick={() => setPath(br.data?.roots ? "" : (parent && br.data && !br.data.dirs.length ? parent : parent))}>↩ ..</button>}
              {br.isLoading && <div className="p-3"><Skeleton className="h-6" /></div>}
              {br.isError && <div className="p-3 text-sm text-bad">{(br.error as Error).message}</div>}
              {br.data?.dirs.map((p) => <button key={p} className="w-full text-left px-3 py-2 text-sm hover:bg-s3 flex items-center gap-2" onClick={() => setPath(p)}><FolderOpen size={14} className="text-dim" />{p.split("/").pop() || p}</button>)}
              {br.data && !br.data.dirs.length && !br.isLoading && <div className="p-3 text-sm text-dim">Sem subpastas</div>}
            </div>
          </div>
        </Field>
        {path && br.data && !br.data.roots && <div className="text-xs text-mute flex gap-2 items-center"><Eye size={13} />Pasta selecionada: <code className="text-fg">{path}</code> {br.data.writable ? <Badge tone="warn">montada com escrita</Badge> : <Badge tone="ok">montada só de leitura</Badge>}</div>}
        <Toggle checked={writable} onChange={setWritable} disabled={!br.data?.writable} label="Permitir escrita de legendas nesta biblioteca" hint={br.data?.writable ? "Só serão criados ficheiros de legenda; vídeos nunca são alterados." : "Disponível apenas se a pasta estiver montada com permissão de escrita."} />
      </div>
    </Modal>
  );
}

function Integration({ name, d, set }: { name: "plex" | "radarr" | "sonarr"; d: Settings; set: (k: string, v: any) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const info = useQuery({ queryKey: ["integrations"], queryFn: () => api.get<Record<string, any>>("/integrations") });
  const [secret, setSecret] = useState("");
  const cfg = d[name];
  const secretKey = name === "plex" ? "plex_token" : `${name}_api_key`;
  const upd = (patch: any) => set(name, { ...cfg, ...patch });
  const saveSecret = useMutation({ mutationFn: () => api.put("/secrets", { values: { [secretKey]: secret.trim() } }), onSuccess: () => { setSecret(""); qc.invalidateQueries({ queryKey: ["integrations"] }); toast("ok", "Segredo guardado"); }, onError: (e: Error) => toast("bad", e.message) });
  const test = useMutation({
    mutationFn: async () => {
      if (secret.trim()) { await api.put("/secrets", { values: { [secretKey]: secret.trim() } }); setSecret(""); qc.invalidateQueries({ queryKey: ["integrations"] }); }
      await api.put("/settings", { [name]: cfg });
      return api.post<any>(`/integrations/${name}/test`);
    },
    onSuccess: (r) => toast(r.ok ? "ok" : "bad", r.ok ? `Ligação OK${r.version ? ` (v${r.version})` : ""}` : r.message), onError: (e: Error) => toast("bad", e.message),
  });
  const sync = useStartJob();
  const label = { plex: "Plex", radarr: "Radarr (filmes)", sonarr: "Sonarr (séries)" }[name];
  const si = info.data?.[name];
  return (
    <Section title={label} hint="Opcional. Fornece IDs, posters e correspondência; a aplicação funciona sem esta integração."
      actions={<Toggle checked={!!cfg.enabled} onChange={(v) => upd({ enabled: v })} label="Ativo" />}>
      {cfg.enabled && (
        <div className="space-y-4">
          <div className="grid sm:grid-cols-2 gap-3">
            <Field label="URL"><input className="input" value={cfg.url} placeholder={name === "plex" ? "http://192.168.1.10:32400" : name === "radarr" ? "http://192.168.1.10:7878" : "http://192.168.1.10:8989"} onChange={(e) => upd({ url: e.target.value })} /></Field>
            <Field label={name === "plex" ? "Token" : "API key"} hint={si?.secret_set ? `Definido (${si.secret_source === "env" ? "ambiente" : "guardado"})` : "Não definido"}>
              <div className="flex gap-2"><input className="input" type="password" autoComplete="off" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder={si?.secret_set ? "•••••••• (vazio = manter)" : ""} disabled={si?.secret_source === "env"} />
                <button className="btn-ghost" disabled={!secret || saveSecret.isPending} onClick={() => saveSecret.mutate()}>Guardar</button></div></Field>
          </div>
          <Field label="Mapeamento de caminhos" hint="Se o serviço vê /data/movies mas esta app vê /media/Filmes, mapeie aqui (remoto → local).">
            <div className="space-y-2">
              {(cfg.path_mappings ?? []).map((m: any, i: number) => (
                <div key={i} className="flex gap-2 items-center">
                  <input className="input font-mono" placeholder="/data/movies" value={m.remote} onChange={(e) => upd({ path_mappings: cfg.path_mappings.map((x: any, j: number) => j === i ? { ...x, remote: e.target.value } : x) })} />→
                  <input className="input font-mono" placeholder="/media/Filmes" value={m.local} onChange={(e) => upd({ path_mappings: cfg.path_mappings.map((x: any, j: number) => j === i ? { ...x, local: e.target.value } : x) })} />
                  <button className="btn-quiet !w-8 !p-0" aria-label="Remover" onClick={() => upd({ path_mappings: cfg.path_mappings.filter((_: any, j: number) => j !== i) })}><X size={14} /></button>
                </div>
              ))}
              <button className="btn-ghost !h-8" onClick={() => upd({ path_mappings: [...(cfg.path_mappings ?? []), { remote: "", local: "" }] })}><Plus size={14} />Adicionar mapeamento</button>
            </div>
          </Field>
          <div className="flex gap-2 justify-end">
            <button className="btn-ghost" disabled={test.isPending} onClick={() => test.mutate()}>{test.isPending && <Loader2 size={14} className="animate-spin" />}Testar ligação</button>
            <button className="btn-ghost" onClick={async () => { try { if (secret.trim()) { await api.put("/secrets", { values: { [secretKey]: secret.trim() } }); setSecret(""); } await api.put("/settings", { [name]: cfg }); qc.invalidateQueries({ queryKey: ["integrations"] }); sync.mutate({ url: "/integrations/sync" }); } catch (e) { toast("bad", (e as Error).message); } }}>Sincronizar metadados</button>
          </div>
        </div>
      )}
    </Section>
  );
}
