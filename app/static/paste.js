// Único script do painel: permite colar (Ctrl+V) ou arrastar um print para o campo de pergunta.
// Ele só copia a imagem da área de transferência para o campo de arquivo do formulário.
(function () {
  var form = document.getElementById("ask-form");
  if (!form) return;
  var input = form.querySelector('input[type="file"]');
  var label = document.getElementById("ask-file-name");

  function show() {
    if (!label) return;
    var f = input.files && input.files[0];
    label.textContent = f ? "Anexo: " + f.name + " (" + Math.ceil(f.size / 1024) + " KB)" : "";
  }

  function take(files) {
    if (!files || !files.length) return false;
    var file = files[0];
    if (file.type.indexOf("image/") === 0 && (!file.name || file.name === "image.png")) {
      file = new File([file], "print-colado.png", { type: file.type });
    }
    var dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    show();
    return true;
  }

  form.addEventListener("paste", function (e) {
    if (e.clipboardData && take(e.clipboardData.files)) e.preventDefault();
  });
  form.addEventListener("dragover", function (e) { e.preventDefault(); });
  form.addEventListener("drop", function (e) {
    if (e.dataTransfer && take(e.dataTransfer.files)) e.preventDefault();
  });
  input.addEventListener("change", show);
})();
