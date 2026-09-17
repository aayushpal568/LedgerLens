from pathlib import Path
from openpyxl import Workbook

root = Path(__file__).resolve().parent / "final_ui_synthetic"
root.mkdir(parents=True, exist_ok=True)

exact = "Date,Amount,Vendor,Formula\n2024-01-01,100.00,Exact Vendor,=SUM(A1:A2)\n"
(root / "exact original 2024.csv").write_text(exact, encoding="utf-8")
(root / "exact copy 2024.csv").write_text(exact, encoding="utf-8")

near_a = "Invoice 2024\nVendor,Near Duplicate Ltd\nAmount,123.00\nReference,ABC-001\nFormula,+123\n"
near_b = "Invoice 2024\nVendor,Near Duplicate Ltd\nAmount,123.00\nReference,ABC-001\nFormula,+124\n"
(root / "near & special 2024.csv").write_text(near_a, encoding="utf-8")
(root / "near + special 2024.csv").write_text(near_b, encoding="utf-8")

(root / "wrong_period_2021.csv").write_text(
    "Profit and Loss 2021\nRevenue,5000\nExpenses,3000\nFormula,-123\n", encoding="utf-8"
)
(root / "empty @test 2024.csv").write_bytes(b"")
(root / "normal valid 2024.csv").write_text(
    "Date,Description,Amount\n2024-02-01,Normal synthetic transaction,42.50\n", encoding="utf-8"
)
(root / "special (client) & report 2024.csv").write_text(
    "Synthetic special characters < > & and spreadsheet text @test\n", encoding="utf-8"
)

wb = Workbook()
ws = wb.active
ws.title = "Synthetic"
ws.append(["Balance Sheet 2024", "Value"])
ws.append(["Assets", 1000])
ws.append(["Formula-like text", "=SUM(A1:A2)"])
wb.save(root / "normal balance sheet 2024.xlsx")

print(root)
print(len(list(root.iterdir())))
