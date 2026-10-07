function ArchiveIcon({ name, size = 18, className = "" }) {
  const paths = {
    search: <><circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.4-3.4"></path></>,
    filter: <><path d="M4 6h16"></path><path d="M7 12h10"></path><path d="M10 18h4"></path></>,
    check: <path d="m5 12 4 4L19 6"></path>,
    close: <><path d="m6 6 12 12"></path><path d="M18 6 6 18"></path></>,
    menu: <><path d="M4 7h16"></path><path d="M4 12h16"></path><path d="M4 17h16"></path></>,
    panel: <><rect x="3" y="4" width="18" height="16" rx="2"></rect><path d="M9 4v16"></path></>,
    panelRight: <><rect x="3" y="4" width="18" height="16" rx="2"></rect><path d="M15 4v16"></path></>,
    sun: <><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2"></path><path d="M12 20v2"></path><path d="m4.93 4.93 1.42 1.42"></path><path d="m17.66 17.66 1.41 1.41"></path><path d="M2 12h2"></path><path d="M20 12h2"></path><path d="m6.34 17.66-1.41 1.41"></path><path d="m19.07 4.93-1.41 1.41"></path></>,
    moon: <path d="M20.4 14.4A8.5 8.5 0 0 1 9.6 3.6 8.6 8.6 0 1 0 20.4 14.4Z"></path>,
    branch: <><circle cx="6" cy="5" r="2"></circle><circle cx="18" cy="7" r="2"></circle><circle cx="18" cy="17" r="2"></circle><path d="M8 5h2a4 4 0 0 1 4 4v4a4 4 0 0 0 4 4"></path><path d="M8 5h3a4 4 0 0 1 4 4v0a2 2 0 0 0 2 2h1"></path></>,
    chevron: <path d="m7 10 5 5 5-5"></path>,
    paperclip: <path d="m20.5 11.5-8.8 8.8a5 5 0 0 1-7.1-7.1l9.2-9.2a3.5 3.5 0 0 1 5 5l-9.2 9.2a2 2 0 1 1-2.8-2.8l8.5-8.5"></path>,
    copy: <><rect x="8" y="8" width="12" height="12" rx="2"></rect><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"></path></>,
    source: <><ellipse cx="12" cy="5" rx="8" ry="3"></ellipse><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"></path><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"></path></>,
    layers: <><path d="m12 3 9 5-9 5-9-5 9-5Z"></path><path d="m3 12 9 5 9-5"></path><path d="m3 16 9 5 9-5"></path></>,
    shield: <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"></path>,
    file: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"></path><path d="M14 2v6h6"></path></>,
    clock: <><circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path></>,
    hash: <><path d="M10 3 8 21"></path><path d="m16 3-2 18"></path><path d="M4 9h16"></path><path d="M3 15h16"></path></>,
    more: <><circle cx="5" cy="12" r="1"></circle><circle cx="12" cy="12" r="1"></circle><circle cx="19" cy="12" r="1"></circle></>,
    arrow: <><path d="M5 12h14"></path><path d="m13 6 6 6-6 6"></path></>,
    archive: <><path d="M4 7h16"></path><path d="M5 7v13h14V7"></path><path d="M3 3h18v4H3z"></path><path d="M9 11h6"></path></>,
    info: <><circle cx="12" cy="12" r="9"></circle><path d="M12 11v5"></path><path d="M12 8h.01"></path></>,
    chart: <><path d="M4 19V9"></path><path d="M10 19V5"></path><path d="M16 19v-7"></path><path d="M22 19H2"></path></>,
    activity: <path d="M3 12h4l2-6 4 12 2-6h6"></path>,
    flask: <><path d="M9 3h6"></path><path d="M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3"></path><path d="M8 15h8"></path></>
  };

  return (
    <svg
      aria-hidden="true"
      className={`icon ${className}`.trim()}
      width={size}
      height={size}
      viewBox="0 0 24 24"
    >
      {paths[name] || paths.info}
    </svg>
  );
}

Object.assign(window, {
  ArchiveIcon
});
