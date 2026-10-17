import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Job, Meta, Settings } from "./api";
import { useToast } from "./components/ui";

export const useMeta = () => useQuery({ queryKey: ["meta"], queryFn: () => api.get<Meta>("/meta"), staleTime: 60_000 });
export const useSettings = () => useQuery({ queryKey: ["settings"], queryFn: () => api.get<Settings>("/settings") });
export const useSidebar = () => useQuery({ queryKey: ["sidebar"], queryFn: () => api.get<{ missing: number; missing_by_lang: Record<string, number>; empty_subtitles: number; active_jobs: number; pending_downloads: number }>("/sidebar"), refetchInterval: 5000 });

export function useStartJob() {
  const qc = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: ({ url, body }: { url: string; body?: unknown }) => api.post<{ job_id: number }>(url, body),
    onSuccess: (r) => { toast("info", `Tarefa #${r.job_id} iniciada`); qc.invalidateQueries({ queryKey: ["jobs"] }); qc.invalidateQueries({ queryKey: ["sidebar"] }); },
    onError: (e: Error) => toast("bad", e.message),
  });
}

/** Quando uma tarefa termina, invalida os dados de biblioteca. */
export function useJobWatcher() {
  const qc = useQueryClient();
  return useQuery({
    queryKey: ["jobs", "active"],
    queryFn: async () => {
      const r = await api.get<{ items: Job[] }>("/jobs?limit=5");
      const running = r.items.some((j) => j.status === "running" || j.status === "queued");
      const last = (window as any).__wasRunning as boolean | undefined;
      if (last && !running) qc.invalidateQueries();
      (window as any).__wasRunning = running;
      return r.items;
    },
    refetchInterval: 2500,
  });
}
