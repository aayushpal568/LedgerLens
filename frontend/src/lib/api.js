import axios from "axios";

const BASE = (process.env.REACT_APP_BACKEND_URL || "http://127.0.0.1:8001").replace(/\/+$/, "");
export const API = `${BASE}/api`;

const http = axios.create({ baseURL: API, timeout: 30000 });

export const api = {
  getFirm: () => http.get("/firm").then((r) => r.data),
  updateFirm: (body) => http.put("/firm", body).then((r) => r.data),

  listClients: () => http.get("/clients").then((r) => r.data),
  createClient: (body) => http.post("/clients", body).then((r) => r.data),
  deleteClient: (id) => http.delete(`/clients/${id}`).then((r) => r.data),

  listFiles: (id) => http.get(`/clients/${id}/files`).then((r) => r.data),
  uploadFiles: (id, formData, onUploadProgress) =>
    http.post(`/clients/${id}/files`, formData, { onUploadProgress }).then((r) => r.data),
  deleteFile: (cid, fid) => http.delete(`/clients/${cid}/files/${fid}`).then((r) => r.data),

  listTemplates: () => http.get("/checklist-templates").then((r) => r.data),
  createTemplate: (body) => http.post("/checklist-templates", body).then((r) => r.data),
  updateTemplate: (id, body) => http.put(`/checklist-templates/${id}`, body).then((r) => r.data),
  deleteTemplate: (id) => http.delete(`/checklist-templates/${id}`).then((r) => r.data),

  startScan: (id, body) => http.post(`/clients/${id}/scan`, body).then((r) => r.data),
  cancelScan: (id) => http.post(`/scans/${id}/cancel`).then((r) => r.data),
  listScans: (id) => http.get(`/clients/${id}/scans`).then((r) => r.data),
  getScan: (id) => http.get(`/scans/${id}`).then((r) => r.data),
  getFindings: (id, params) => http.get(`/scans/${id}/findings`, { params }).then((r) => r.data),
  updateFinding: (id, body) => http.patch(`/findings/${id}`, body).then((r) => r.data),
  reportUrl: (scanId, format) => `${API}/scans/${scanId}/report?format=${format}`,
  downloadReport: (scanId, format) => http.get(`/scans/${scanId}/report`, {
    params: { format }, responseType: "arraybuffer",
  }).then((r) => r.data),
};
