"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

export function StockSearch() {
  const router = useRouter();
  const [symbol, setSymbol] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalized = symbol.trim();
    if (!/^\d{6}$/.test(normalized)) {
      setError("请输入 6 位股票代码");
      return;
    }
    setError(null);
    router.push(`/stocks/${normalized}`);
  };

  return (
    <form className="stockSearch" onSubmit={submit} noValidate>
      <label htmlFor="stock-symbol">股票代码</label>
      <div>
        <input
          id="stock-symbol"
          inputMode="numeric"
          autoComplete="off"
          spellCheck={false}
          name="symbol"
          pattern="[0-9]{6}"
          maxLength={6}
          placeholder="例如 300476…"
          value={symbol}
          onChange={(event) => setSymbol(event.target.value.replace(/\D/g, ""))}
          aria-describedby={error ? "stock-symbol-error" : "stock-symbol-help"}
          aria-invalid={Boolean(error)}
        />
        <button type="submit" className="primaryButton">打开个股</button>
      </div>
      <p id={error ? "stock-symbol-error" : "stock-symbol-help"} className={error ? "formError" : undefined}>
        {error ?? "支持沪、深、北交易所 6 位代码；页面只读，不提供下单入口。"}
      </p>
    </form>
  );
}
