import React, { useState, useEffect } from "react";
import { api } from "@/lib/api";
import { useApp } from "@/context/AppContext";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { toast } from "sonner";
import {
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Play,
  RefreshCw,
  ArrowRight,
  ShieldCheck,
  Cpu,
  HardDrive,
  Database,
  BrainCircuit,
  Eye,
  Lock,
  Server,
  FolderLock,
  MemoryStick
} from "lucide-react";

export default function SystemCheck() {
  const { setTab } = useApp();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [setupState, setSetupState] = useState({ in_progress: false, step: "idle", message: "", percent: 0, error: null });

  const startSetup = async () => {
    try {
      toast.info("Starting automated local AI setup (Ollama + Qwen2:0.5B)...");
      const res = await api.setupOllamaQwen();
      setSetupState(res);
    } catch (err) {
      toast.error("Failed to initiate automated setup: " + (err.response?.data?.detail || err.message));
    }
  };

  useEffect(() => {
    let interval = null;
    if (setupState.in_progress) {
      interval = setInterval(async () => {
        try {
          const res = await api.getSetupStatus();
          setSetupState(res);
          if (!res.in_progress) {
            clearInterval(interval);
            if (res.error) {
              toast.error("Setup failed: " + res.error);
            } else {
              toast.success("Local AI & Ollama setup completed successfully!");
              fetchCheck();
            }
          }
        } catch (err) {
          // ignore transient poll error
        }
      }, 2000);
    }
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [setupState.in_progress]);

  const fetchCheck = async () => {
    setLoading(true);
    try {
      const res = await api.getSystemCheck();
      setData(res);
    } catch (err) {
      toast.error("Failed to connect to backend for System Check");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchCheck();
  }, []);

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      toast.info("Running live system test on synthetic document...");
      const res = await api.runSystemTest();
      setTestResult(res);
      const isPass = res.ocr?.status === "PASS" && res.qwen?.status === "PASS";
      if (isPass) {
        toast.success("System self-test passed successfully!");
      } else {
        toast.warning("System self-test finished with warnings or issues.");
      }
      fetchCheck();
    } catch (err) {
      toast.error("System self-test failed to execute: " + (err.response?.data?.detail || err.message));
    } finally {
      setTesting(false);
    }
  };

  const getStatusIcon = (status) => {
    switch (status) {
      case "PASS":
        return <CheckCircle2 className="h-5 w-5 text-emerald-500 shrink-0" />;
      case "WARNING":
        return <AlertTriangle className="h-5 w-5 text-amber-500 shrink-0" />;
      case "FAILED":
      default:
        return <XCircle className="h-5 w-5 text-rose-500 shrink-0" />;
    }
  };

  const getStatusBadge = (status) => {
    switch (status) {
      case "PASS":
        return <Badge variant="outline" className="bg-emerald-500/10 text-emerald-600 border-emerald-500/20 font-semibold">PASS</Badge>;
      case "WARNING":
        return <Badge variant="outline" className="bg-amber-500/10 text-amber-600 border-amber-500/20 font-semibold">WARNING</Badge>;
      case "FAILED":
      default:
        return <Badge variant="outline" className="bg-rose-500/10 text-rose-600 border-rose-500/20 font-semibold">FAILED</Badge>;
    }
  };

  const getCategoryIcon = (key) => {
    switch (key) {
      case "app": return <ShieldCheck className="h-4 w-4 text-primary" />;
      case "backend": return <Server className="h-4 w-4 text-primary" />;
      case "sqlite": return <Database className="h-4 w-4 text-primary" />;
      case "ocr_engine":
      case "ocr_deps": return <Eye className="h-4 w-4 text-primary" />;
      case "ollama":
      case "qwen": return <BrainCircuit className="h-4 w-4 text-primary" />;
      case "api_conn": return <Server className="h-4 w-4 text-primary" />;
      case "disk": return <HardDrive className="h-4 w-4 text-primary" />;
      case "ram": return <MemoryStick className="h-4 w-4 text-primary" />;
      case "hardware": return <Cpu className="h-4 w-4 text-primary" />;
      case "permissions": return <FolderLock className="h-4 w-4 text-primary" />;
      case "offline": return <Lock className="h-4 w-4 text-primary" />;
      default: return <ShieldCheck className="h-4 w-4 text-primary" />;
    }
  };

  const passedCount = data?.checks?.filter(c => c.status === "PASS").length || 0;
  const warningCount = data?.checks?.filter(c => c.status === "WARNING").length || 0;
  const failedCount = data?.checks?.filter(c => c.status === "FAILED").length || 0;

  return (
    <div className="p-8 max-w-6xl mx-auto space-y-8 animate-in fade-in duration-300">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4 border-b border-border pb-6">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold tracking-tight">System Health & Verification</h1>
            {data?.checks && (
              <span className="text-xs font-medium px-2.5 py-1 rounded-full bg-secondary text-foreground">
                {passedCount} Passed · {warningCount} Warnings · {failedCount} Failed
              </span>
            )}
          </div>
          <p className="text-sm text-muted-foreground mt-1">
            Automated verification of desktop runtime, SQLite, local OCR, Ollama AI, hardware resources, and offline security.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Button variant="outline" size="sm" onClick={fetchCheck} disabled={loading}>
            <RefreshCw className={`h-4 w-4 mr-2 ${loading ? "animate-spin" : ""}`} />
            Re-check
          </Button>
          <Button
            size="sm"
            onClick={runTest}
            disabled={testing}
            className="bg-primary text-primary-foreground shadow-sm"
          >
            <Play className={`h-4 w-4 mr-2 ${testing ? "animate-spin" : ""}`} />
            {testing ? "Testing..." : "Run Full System Test"}
          </Button>
          <Button
            variant="default"
            size="sm"
            onClick={() => {
              localStorage.setItem("ledgerlens_first_run_checked", "true");
              setTab("dashboard");
            }}
          >
            Continue to App
            <ArrowRight className="h-4 w-4 ml-2" />
          </Button>
        </div>
      </div>

      {(setupState.in_progress || setupState.error || (data?.checks && data.checks.some(c => (c.id === "ollama" || c.id === "qwen") && c.status !== "PASS"))) && (
        <Card className="border-blue-500/40 bg-blue-500/5">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base font-semibold flex items-center gap-2">
                <BrainCircuit className="h-5 w-5 text-blue-500" />
                One-Click Local AI & Ollama Setup
              </CardTitle>
              {setupState.in_progress ? (
                <Badge variant="outline" className="bg-blue-500/10 text-blue-600 border-blue-500/20 font-semibold animate-pulse">
                  Installing ({setupState.percent}%)
                </Badge>
              ) : (
                <Button
                  size="sm"
                  onClick={startSetup}
                  className="bg-blue-600 hover:bg-blue-700 text-white shadow-sm"
                >
                  <RefreshCw className="h-4 w-4 mr-2" />
                  Auto-Install / Repair AI Components
                </Button>
              )}
            </div>
            <CardDescription className="text-xs">
              Automatically verifies and installs the local Ollama service and lightweight Qwen2:0.5B model without any terminal commands.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-xs">
            {setupState.in_progress ? (
              <div className="space-y-2">
                <div className="flex justify-between text-muted-foreground">
                  <span>{setupState.message}</span>
                  <span>{setupState.percent}%</span>
                </div>
                <div className="w-full bg-secondary h-2 rounded-full overflow-hidden">
                  <div
                    className="bg-blue-600 h-full transition-all duration-300 rounded-full"
                    style={{ width: `${setupState.percent}%` }}
                  />
                </div>
              </div>
            ) : setupState.error ? (
              <p className="text-rose-600 bg-rose-500/10 p-2.5 rounded border border-rose-500/20">
                {setupState.error}
              </p>
            ) : (
              <p className="text-muted-foreground">
                Click above to have LedgerLens automatically configure local Ollama and Qwen2:0.5B completely offline.
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {testResult && (
        <Card className="border-primary/40 bg-primary/5">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base font-semibold flex items-center gap-2">
                {getStatusIcon(testResult.ocr?.status === "PASS" && testResult.qwen?.status === "PASS" ? "PASS" : "WARNING")}
                Live Synthetic System Test Results
              </CardTitle>
              {getStatusBadge(testResult.ocr?.status === "PASS" && testResult.qwen?.status === "PASS" ? "PASS" : "WARNING")}
            </div>
            <CardDescription className="text-xs">
              Synthetic test executed locally on this device with zero external network transmission.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 text-sm">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="p-3.5 rounded-lg border border-border bg-card">
                <div className="flex items-center justify-between mb-2">
                  <span className="font-semibold flex items-center gap-2">
                    <Eye className="h-4 w-4 text-primary" /> Synthetic OCR Test
                  </span>
                  {getStatusBadge(testResult.ocr?.status)}
                </div>
                <p className="text-xs text-muted-foreground">{testResult.ocr?.message}</p>
              </div>

              <div className="p-3.5 rounded-lg border border-border bg-card">
                <div className="flex items-center justify-between mb-2">
                  <span className="font-semibold flex items-center gap-2">
                    <BrainCircuit className="h-4 w-4 text-primary" /> Synthetic Ollama/Qwen Test
                  </span>
                  {getStatusBadge(testResult.qwen?.status)}
                </div>
                <p className="text-xs text-muted-foreground">{testResult.qwen?.message}</p>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {loading && !data ? (
        <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
          <RefreshCw className="h-8 w-8 animate-spin mb-4 text-primary" />
          <p className="text-sm font-medium">Running system checks...</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {data?.checks?.map((check) => (
            <Card key={check.id} className="hover:border-border/80 transition-all flex flex-col justify-between">
              <CardHeader className="pb-2">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <div className="p-1.5 rounded-md bg-secondary/80">
                      {getCategoryIcon(check.id)}
                    </div>
                    <CardTitle className="text-sm font-semibold">{check.name}</CardTitle>
                  </div>
                  {getStatusBadge(check.status)}
                </div>
              </CardHeader>
              <CardContent className="pt-2">
                <p className="text-xs text-muted-foreground leading-relaxed">
                  {check.explanation || check.details}
                </p>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <div className="p-4 rounded-lg bg-secondary/60 border border-border text-xs flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Lock className="h-4 w-4 text-primary shrink-0" />
          <span className="text-muted-foreground">
            LedgerLens is running in <strong>100% offline local mode</strong>. All files remain on this device. No documents or accounting data are transmitted over the network.
          </span>
        </div>
      </div>
    </div>
  );
}
