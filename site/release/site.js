/* heartbeat-framework.org — theme v2 behaviour. No dependencies. */
(function(){
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* mobile menu */
  var b = document.querySelector('.burger');
  if (b) b.addEventListener('click', function(){
    var open = document.documentElement.classList.toggle('menu-open');
    b.setAttribute('aria-expanded', open ? 'true' : 'false');
    document.body.style.overflow = open ? 'hidden' : '';
  });

  /* reveal on enter */
  var els = document.querySelectorAll('.reveal');
  if (els.length && 'IntersectionObserver' in window && !reduce) {
    var io = new IntersectionObserver(function(en){
      en.forEach(function(e){ if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, {rootMargin: '0px 0px -8% 0px', threshold: 0.05});
    els.forEach(function(el){ io.observe(el); });
  } else {
    els.forEach(function(el){ el.classList.add('in'); });
  }

  /* scroll-scrubbed word fill */
  var scrubs = Array.prototype.slice.call(document.querySelectorAll('.scrub'));
  scrubs.forEach(function(p){
    var text = p.textContent.trim().split(/\s+/);
    p.innerHTML = text.map(function(w){ return '<span class="w">' + w + '</span>'; }).join(' ');
  });
  if (scrubs.length && !reduce) {
    var ticking = false;
    function update(){
      ticking = false;
      var vh = window.innerHeight;
      scrubs.forEach(function(p){
        var r = p.getBoundingClientRect();
        var start = vh * 0.85, end = vh * 0.35;
        var prog = (start - r.top) / (start - end);
        prog = Math.max(0, Math.min(1, prog));
        var words = p.querySelectorAll('.w'), n = words.length;
        for (var i = 0; i < n; i++) {
          var on = prog * (n + 2) > i + 1;
          if (on !== words[i].classList.contains('on')) words[i].classList.toggle('on', on);
        }
      });
    }
    window.addEventListener('scroll', function(){ if (!ticking) { ticking = true; requestAnimationFrame(update); } }, {passive: true});
    window.addEventListener('resize', update);
    update();
  } else {
    scrubs.forEach(function(p){ p.querySelectorAll('.w').forEach(function(w){ w.classList.add('on'); }); });
  }
})();
