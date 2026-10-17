import { useState } from "react";
import { Lock, Subtitles } from "lucide-react";
import { api } from "../api";

export function Login({ onDone }: { onDone: () => void }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="h-full grid place-items-center p-4">
      <form className="card w-full max-w-sm p-7 space-y-5 animate-rise shadow-pop" onSubmit={async (e) => {
        e.preventDefault(); setBusy(true); setErr("");
        try { await api.post("/auth/login", { password: pw }); onDone(); } catch (x) { setErr((x as Error).message); } finally { setBusy(false); }
      }}>
        <div className="flex items-center gap-2.5"><div className="w-8 h-8 rounded-lg bg-accent grid place-items-center text-black"><Subtitles size={18} strokeWidth={2.4} /></div><div className="font-semibold text-lg">SubSync <span className="text-accent">Pro</span></div></div>
        <div className="space-y-1.5"><label className="label" htmlFor="pw">Palavra-passe</label>
          <div className="relative"><Lock size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-dim" /><input id="pw" type="password" autoFocus className="input pl-9" value={pw} onChange={(e) => setPw(e.target.value)} /></div></div>
        {err && <div className="text-sm text-bad">{err}</div>}
        <button className="btn-primary w-full" disabled={busy || !pw}>Entrar</button>
      </form>
    </div>
  );
}
