import React, { createContext, useContext, useState, useEffect, useCallback } from "react";
import { api } from "@/lib/api";

const AppContext = createContext(null);
export const useApp = () => useContext(AppContext);

export function AppProvider({ children }) {
  const [firm, setFirm] = useState(null);
  const [clients, setClients] = useState([]);
  const [activeClient, setActiveClient] = useState(null);
  const [activeScan, setActiveScan] = useState(null);
  const [tab, setTab] = useState("dashboard");
  const [compareFinding, setCompareFinding] = useState(null);
  const [dark, setDark] = useState(false);

  const refreshFirm = useCallback(async () => setFirm(await api.getFirm()), []);
  const refreshClients = useCallback(async () => {
    try {
      const data = await api.listClients();
      const list = Array.isArray(data) ? data : [];
      setClients(list);
      return list;
    } catch {
      setClients([]);
      return [];
    }
  }, []);

  useEffect(() => {
    let active = true;
    const initData = async () => {
      // Retry connecting on startup
      for (let attempt = 0; attempt < 30; attempt++) {
        try {
          const [f, c] = await Promise.all([api.getFirm(), api.listClients()]);
          if (!active) return;
          setFirm(f || null);
          setClients(Array.isArray(c) ? c : []);
          return;
        } catch {
          if (!active) return;
          await new Promise((resolve) => setTimeout(resolve, 1000));
        }
      }
    };
    initData();
    return () => { active = false; };
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);

  const goCompare = (finding) => {
    setCompareFinding(finding);
    setTab("file_compare");
  };

  const value = {
    firm, setFirm, refreshFirm,
    clients, refreshClients,
    activeClient, setActiveClient,
    activeScan, setActiveScan,
    tab, setTab,
    compareFinding, setCompareFinding, goCompare,
    dark, setDark,
  };
  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
