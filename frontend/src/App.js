import React from "react";
import "@/App.css";
import { Toaster } from "sonner";
import { AuthProvider, useAuth } from "@/context/AuthContext";
import { AppProvider, useApp } from "@/context/AppContext";
import AuthScreen from "@/components/AuthScreen";
import Titlebar from "@/components/Titlebar";
import Sidebar from "@/components/Sidebar";
import Overview from "@/pages/Overview";
import Clients from "@/pages/Clients";
import ScanWorkspace from "@/pages/ScanWorkspace";
import Checklists from "@/pages/Checklists";
import ReviewCenter from "@/pages/ReviewCenter";
import FileCompare from "@/pages/FileCompare";
import Reports from "@/pages/Reports";

function Shell() {
  const { tab } = useApp();

  const pages = {
    dashboard: <Overview />,
    clients: <Clients />,
    scan_workspace: <ScanWorkspace />,
    checklists: <Checklists />,
    review_center: <ReviewCenter />,
    file_compare: <FileCompare />,
    reports: <Reports />,
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

function AppContent() {
  const { isAuthenticated } = useAuth();

  if (!isAuthenticated) {
    return <AuthScreen />;
  }

  return (
    <AppProvider>
      <Shell />
      <Toaster position="bottom-right" richColors closeButton />
    </AppProvider>
  );
}

export default function App() {
  return (
    <div className="App">
      <AuthProvider>
        <AppContent />
      </AuthProvider>
    </div>
  );
}
