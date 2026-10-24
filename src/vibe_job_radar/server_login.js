"use strict";
document.getElementById("login").addEventListener("submit", async event => {
  event.preventDefault();
  const button = document.getElementById("submit");
  const input = document.getElementById("password");
  const error = document.getElementById("error");
  button.disabled = true; error.textContent = "";
  try {
    const response = await fetch("/api/session/login", {method:"POST", cache:"no-store",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({password:input.value})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "登录未通过。");
    sessionStorage.removeItem("radar-session"); location.replace("/");
  } catch (failure) {error.textContent = failure.message;}
  finally {input.value = ""; button.disabled = false;}
});
