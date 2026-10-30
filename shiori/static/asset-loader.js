"use strict";

// Only these shipped pages may request code. Never load a path supplied by a
// page ID, plugin, API response or user input; CSP remains script-src 'self'.
window.ShioriAssets = (() => {
  const pages = Object.freeze({
    qqChats: {script:'qq-monitor.js',css:'qq-monitor.css',global:'ShioriQQMonitor'},
    statistics: {script:'statistics.js',css:'statistics.css',global:'ShioriStatistics',beforeTheme:true},
    plugins: {script:'plugins.js',global:'ShioriPlugins'},
    characterSchedule: {script:'character-schedule.js',css:'character-schedule.css',global:'ShioriCharacterSchedule'},
  });
  const suffix = new URL(document.currentScript.src).search;
  const resources = new Map(), pending = new Map();
  function load(name, css, beforeTheme) {
    if(resources.has(name))return resources.get(name);
    const promise = new Promise((resolve,reject) => {
      const node=document.createElement(css?'link':'script');
      if(css){node.rel='stylesheet';node.href='/static/'+name+suffix;}
      else{node.src='/static/'+name+suffix;node.async=true;}
      node.onload=()=>resolve();
      node.onerror=()=>{node.remove();resources.delete(name);reject(new Error('页面资源加载失败，请重新加载。'));};
      const theme=css && beforeTheme && document.querySelector('link[href^="/static/rhine.css"]');
      if(theme)document.head.insertBefore(node,theme);else document.head.appendChild(node);
    });
    resources.set(name,promise);
    return promise;
  }
  async function ensure(page) {
    if(!Object.hasOwn(pages,page))return;
    if(pending.has(page))return pending.get(page);
    const config=pages[page];
    const task=(async()=>{
      // Style failures do not execute a controller with missing presentation.
      if(config.css)await load(config.css,true,config.beforeTheme);
      await load(config.script,false);
      if(!window[config.global])throw new Error('页面没有正确启动，请刷新后重试。');
    })();
    pending.set(page,task);
    try{await task;}catch(error){pending.delete(page);throw error;}
  }
  return {ensure};
})();
