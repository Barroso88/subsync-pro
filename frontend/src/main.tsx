import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import "./index.css";
import { api } from "./api";
import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/ui";
import { Login } from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import Movies from "./pages/Movies";
import Series from "./pages/Series";
import Episodes from "./pages/Episodes";
import Missing from "./pages/Missing";
import SearchPage from "./pages/SearchPage";
import Downloads from "./pages/Downloads";
import Jobs from "./pages/Jobs";
import Providers from "./pages/Providers";
import SettingsPage from "./pages/Settings";
import EmptySubs from "./pages/EmptySubs";
import Convert from "./pages/Convert";

const qc = new QueryClient({ defaultOptions: { queries: { staleTime: 15_000, retry: 1, refetchOnWindowFocus: false } } });

function Gate() {
  const st = useQuery({ queryKey: ["auth"], queryFn: () => api.get<{ auth_required: boolean; authenticated: boolean }>("/auth/status") });
  const [forced, setForced] = useState(false);
  useEffect(() => { const h = () => { setForced(true); st.refetch(); }; window.addEventListener("subsync:unauth", h); return () => window.removeEventListener("subsync:unauth", h); }, []);
  if (st.isLoading) return null;
  if (st.data && st.data.auth_required && (!st.data.authenticated || forced && !st.data.authenticated)) return <Login onDone={() => { setForced(false); qc.invalidateQueries(); }} />;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="filmes" element={<Movies />} />
        <Route path="series" element={<Series />} />
        <Route path="episodios" element={<Episodes />} />
        <Route path="em-falta" element={<Missing />} />
        <Route path="legendas-vazias" element={<EmptySubs />} />
        <Route path="converter" element={<Convert />} />
        <Route path="procurar" element={<SearchPage />} />
        <Route path="downloads" element={<Downloads />} />
        <Route path="tarefas" element={<Jobs />} />
        <Route path="fornecedores" element={<Providers />} />
        <Route path="definicoes" element={<SettingsPage />} />
      </Route>
    </Routes>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={qc}><ToastProvider><BrowserRouter><Gate /></BrowserRouter></ToastProvider></QueryClientProvider>
  </React.StrictMode>
);
