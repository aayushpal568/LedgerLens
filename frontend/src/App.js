import React from "react";
import "@/App.css";
import { Toaster } from "sonner";
import { AppProvider, useApp } from "@/context/AppContext";
import Titlebar from "@/components/Titlebar";
import Sidebar from "@/components/Sidebar";
import Overview from "@/pages/Overview";
import Clients from "@/pages/Clients";
import ScanWorkspace from "@/pages/ScanWorkspace";
import Checklists from "@/pages/Checklists";
import ReviewCenter from "@/pages/ReviewCenter";
import FileCompare from "@/pages/FileCompare";
import Reports from "@/pages/Reports";
import SystemCheck from "@/pages/SystemCheck";

function Shell() {
  const { tab, setTab } = useApp();

  React.useEffect(() => {
    const hasRun = localStorage.getItem("ledgerlens_first_run_checked");
    if (!hasRun) {
      setTab("system_check");
    }
  }, [setTab]);

  const pages = {
    dashboard: <Overview />,
    clients: <Clients />,
    scan_workspace: <ScanWorkspace />,
    checklists: <Checklists />,
    review_center: <ReviewCenter />,
    file_compare: <FileCompare />,
    reports: <Reports />,
    system_check: <SystemCheck />,
  };
  return (
    <div className="h-screen flex flex-col bg-background text-foreground">
      <Titlebar />
      <div className="flex-1 flex min-h-0">
        <Sidebar />
        <main className="flex-1 min-w-0 overflow-y-auto" data-testid="page-content">
          {pages[tab] || <Overview />}
        </main>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <div className="App">
      <AppProvider>
        <Shell />
        <Toaster position="bottom-right" richColors closeButton />
      </AppProvider>
    </div>
  );
}
