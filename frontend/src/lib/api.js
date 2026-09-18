import axios from "axios";

const BASE = (process.env.REACT_APP_BACKEND_URL || "http://127.0.0.1:8001").replace(/\/+$/, "");
export const API = `${BASE}/api`;

const http = axios.create({ baseURL: API, timeout: 30000 });

let _accessToken = null;
let _onUnauthorizedCallback = null;

export const setAccessToken = (token) => {
  _accessToken = token || null;
};

export const getAccessToken = () => _accessToken;

export const setOnUnauthorized = (cb) => {
  _onUnauthorizedCallback = cb;
};

http.interceptors.request.use(
  (config) => {
    if (_accessToken) {
      config.headers.Authorization = `Bearer ${_accessToken}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

http.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response && error.response.status === 401) {
      if (_onUnauthorizedCallback) {
        _onUnauthorizedCallback();
      }
    }
    return Promise.reject(error);
  }
);

export const api = {
  // Auth
  signup: (body) => http.post("/auth/signup", body).then((r) => r.data),
  login: (body) => http.post("/auth/login", body).then((r) => r.data),
  refresh: (body) => http.post("/auth/refresh", body).then((r) => r.data),
  logout: () => http.post("/auth/logout").then((r) => r.data),
  getMe: () => http.get("/auth/me").then((r) => r.data),

  // Firm
  getFirm: () => http.get("/firm").then((r) => r.data),
  updateFirm: (body) => http.put("/firm", body).then((r) => r.data),

  // Clients
  listClients: () => http.get("/clients").then((r) => r.data),
  createClient: (body) => http.post("/clients", body).then((r) => r.data),
  deleteClient: (id) => http.delete(`/clients/${id}`).then((r) => r.data),

  // Files
  listFiles: (id) => http.get(`/clients/${id}/files`).then((r) => r.data),
  uploadFiles: (id, formData, onUploadProgress) =>
    http.post(`/clients/${id}/files`, formData, { onUploadProgress }).then((r) => r.data),
  deleteFile: (cid, fid) => http.delete(`/clients/${cid}/files/${fid}`).then((r) => r.data),

  // Templates
  listTemplates: () => http.get("/checklist-templates").then((r) => r.data),
  createTemplate: (body) => http.post("/checklist-templates", body).then((r) => r.data),
  updateTemplate: (id, body) => http.put(`/checklist-templates/${id}`, body).then((r) => r.data),
  deleteTemplate: (id) => http.delete(`/checklist-templates/${id}`).then((r) => r.data),

  // Scans & findings
  startScan: (id, body) => http.post(`/clients/${id}/scan`, body).then((r) => r.data),
  cancelScan: (id) => http.post(`/scans/${id}/cancel`).then((r) => r.data),
  listScans: (id) => http.get(`/clients/${id}/scans`).then((r) => r.data),
  getScan: (id) => http.get(`/scans/${id}`).then((r) => r.data),
  getFindings: (id, params) => http.get(`/scans/${id}/findings`, { params }).then((r) => r.data),
  updateFinding: (id, body) => http.patch(`/findings/${id}`, body).then((r) => r.data),
  reportUrl: (scanId, format) => `${API}/scans/${scanId}/report?format=${format}`,
  downloadReport: (scanId, format) =>
    http.get(`/scans/${scanId}/report`, {
      params: { format },
      responseType: "arraybuffer",
    }).then((r) => r.data),

  // Agent
  postAgentMessage: (body) => http.post("/agent/messages", body).then((r) => r.data),
  getAgentRun: (runId) => http.get(`/agent/runs/${runId}`).then((r) => r.data),
  cancelAgentRun: (runId) => http.post(`/agent/runs/${runId}/cancel`).then((r) => r.data),
  listAgentApprovals: (params) => http.get("/agent/approvals", { params }).then((r) => r.data),
  getAgentApproval: (approvalId) => http.get(`/agent/approvals/${approvalId}`).then((r) => r.data),
  approveAgentApproval: (approvalId) => http.post(`/agent/approvals/${approvalId}/approve`).then((r) => r.data),
  rejectAgentApproval: (approvalId, body) => http.post(`/agent/approvals/${approvalId}/reject`, body).then((r) => r.data),
  listAgentMessages: (threadId) => http.get(`/agent/threads/${threadId}/messages`).then((r) => r.data),
  listAgentRunSteps: (runId) => http.get(`/agent/runs/${runId}/steps`).then((r) => r.data),
};
