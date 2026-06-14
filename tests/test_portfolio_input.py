import tempfile
import unittest
from pathlib import Path

from quant_trend.portfolio_input import portfolio_from_csv_source, portfolio_from_text


class PortfolioInputTests(unittest.TestCase):
    def test_parse_natural_language_portfolio(self):
        text = """
        账户净值 10万 现金 -5万 融资 5万 维持保证金 6万 目标杠杆 1.3x
        MU 300股 成本115 intact 信心1 核心 不主动卖
        AAOI 500股 成本20 watch conviction 0.8
        INTC 400 shares avg 31 broken
        """

        portfolio = portfolio_from_text(text)

        self.assertEqual(portfolio.account_equity, 100000)
        self.assertEqual(portfolio.cash, -50000)
        self.assertEqual(portfolio.margin_debit, 50000)
        self.assertEqual(portfolio.maintenance_margin, 60000)
        self.assertEqual(portfolio.target_gross_hint, 1.3)
        self.assertEqual(portfolio.positions["MU"].shares, 300)
        self.assertEqual(portfolio.positions["MU"].bucket, "core")
        self.assertEqual(portfolio.positions["MU"].trade_constraint, "soft_no_reduce")
        self.assertEqual(portfolio.positions["AAOI"].thesis_status, "watch")
        self.assertEqual(portfolio.positions["INTC"].thesis_status, "broken")

    def test_parse_csv_portfolio(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "portfolio.csv"
            path.write_text(
                "symbol,shares,avg_cost,thesis_status,conviction\n"
                "MU,300,115,intact,1\n"
                "LITE,180,65,intact,0.9\n",
                encoding="utf-8",
            )

            portfolio = portfolio_from_csv_source(path, account_equity=100000, cash=-40000, margin_debit=40000)

            self.assertEqual(portfolio.positions["MU"].avg_cost, 115)
            self.assertEqual(portfolio.positions["LITE"].conviction, 0.9)
            self.assertEqual(portfolio.cash, -40000)


if __name__ == "__main__":
    unittest.main()
