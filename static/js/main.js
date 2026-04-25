// Tab switching for users page
function showTab(id) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  event.target.closest('.tab-btn').classList.add('active');
}

// Auto-dismiss alerts after 5s
setTimeout(() => {
  document.querySelectorAll('.alert').forEach(a => a.remove());
}, 5000);
