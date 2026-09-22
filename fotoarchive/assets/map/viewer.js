/* All resources use the loopback cache. No external map or photo requests. */
const base=new URL('.',location.href).href;
let bridge=null,ready=false,pendingPoints=[],selectionStart=null;
const theme=new URLSearchParams(location.search).get('theme')==='dark'?'dark':'light';
const map=new maplibregl.Map({container:'map',center:[37.7,55.65],zoom:7,maxZoom:19,
  style:{version:8,glyphs:base+'fonts/{fontstack}/{range}.pbf',sprite:base+'sprites/v4/'+theme,
    sources:{protomaps:{type:'vector',tiles:[base+'tiles/{z}/{x}/{y}.mvt'],minzoom:0,maxzoom:15,
      attribution:'© OpenStreetMap contributors · Protomaps · ESA WorldCover'}},
    layers:basemaps.layers('protomaps',basemaps.namedFlavor(theme),{lang:'ru'})},
  attributionControl:{compact:true},renderWorldCopies:false,fadeDuration:120});
map.addControl(new maplibregl.NavigationControl({showCompass:false}),'top-right');
map.boxZoom.disable();map.dragRotate.disable();map.touchZoomRotate.disableRotation();
function viewChanged(){if(bridge){let b=map.getBounds();bridge.viewport(JSON.stringify([b.getWest(),b.getSouth(),b.getEast(),b.getNorth()]));}}
window.fotoMap={
  points(points){pendingPoints=points;if(!ready)return;map.getSource('photos').setData({type:'FeatureCollection',features:points.map((p,i)=>({type:'Feature',geometry:{type:'Point',coordinates:[p.longitude,p.latitude]},properties:{...p,index:i,label:p.count>=1000?(p.count/1000).toFixed(1)+'к':String(p.count)}}))});},
  fit(bounds){map.fitBounds([[bounds[0],bounds[1]],[bounds[2],bounds[3]]],{padding:24,duration:0,maxZoom:16});},
  theme(value){let u=new URL(location.href);u.searchParams.set('theme',value);u.searchParams.set('view',JSON.stringify([map.getCenter().lng,map.getCenter().lat,map.getZoom()]));location.replace(u);},
  inspect(){return {loaded:map.loaded(),zoom:map.getZoom(),features:map.queryRenderedFeatures().length,buildings:map.queryRenderedFeatures().filter(f=>f.sourceLayer==="buildings").length,center:map.getCenter().toArray()};}
};
map.on('load',()=>{map.addSource('photos',{type:'geojson',data:{type:'FeatureCollection',features:[]}});
  map.addLayer({id:'photo-points',type:'circle',source:'photos',paint:{'circle-radius':['case',['>', ['get','count'],1],14,6],
    'circle-color':['case',['all',['>', ['coalesce',['get','manual_count'],0],0],['>', ['coalesce',['get','metadata_count'],0],0]],'#826bb0',['>', ['coalesce',['get','manual_count'],0],0],'#477bbb','#159e8f'],'circle-stroke-width':1.5,'circle-stroke-color':'#fff'}});
  map.addLayer({id:'photo-counts',type:'symbol',source:'photos',filter:['>', ['get','count'],1],layout:{'text-field':['get','label'],'text-font':['Noto Sans Medium'],'text-size':12,'text-allow-overlap':true},paint:{'text-color':'#fff'}});
  ready=true;fotoMap.points(pendingPoints);const saved=new URLSearchParams(location.search).get('view');if(saved){const v=JSON.parse(saved);map.jumpTo({center:v.slice(0,2),zoom:v[2]});}else map.jumpTo({center:[37.7,55.65],zoom:7});viewChanged();if(bridge)bridge.loaded();
});
map.on('moveend',viewChanged);
map.on('click','photo-points',e=>{if(selectionStart)return;let p=pendingPoints[e.features[0].properties.index];if(bridge&&p)bridge.selected(JSON.stringify([p.west??p.longitude-.002,p.south??p.latitude-.002,p.east??p.longitude+.002,p.north??p.latitude+.002]));});
map.on('mouseenter','photo-points',()=>map.getCanvas().style.cursor='pointer');map.on('mouseleave','photo-points',()=>map.getCanvas().style.cursor='');
const box=document.getElementById('selection');
map.getCanvasContainer().addEventListener('mousedown',e=>{if(e.button!==0||!e.shiftKey)return;e.preventDefault();map.dragPan.disable();selectionStart=[e.offsetX,e.offsetY];box.style.display='block';});
window.addEventListener('mousemove',e=>{if(!selectionStart)return;box.style.left=Math.min(selectionStart[0],e.clientX)+'px';box.style.top=Math.min(selectionStart[1],e.clientY)+'px';box.style.width=Math.abs(selectionStart[0]-e.clientX)+'px';box.style.height=Math.abs(selectionStart[1]-e.clientY)+'px';});
window.addEventListener('mouseup',e=>{if(!selectionStart)return;let a=map.unproject(selectionStart),b=map.unproject([e.clientX,e.clientY]);selectionStart=null;box.style.display='none';map.dragPan.enable();if(bridge)bridge.selected(JSON.stringify([Math.min(a.lng,b.lng),Math.min(a.lat,b.lat),Math.max(a.lng,b.lng),Math.max(a.lat,b.lat)]));});
new QWebChannel(qt.webChannelTransport,channel=>{bridge=channel.objects.bridge;viewChanged();if(ready)bridge.loaded();});
