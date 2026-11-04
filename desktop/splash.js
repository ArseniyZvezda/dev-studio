document.getElementById('status').textContent = new URLSearchParams(location.search).get('message') || '';
