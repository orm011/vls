import numpy as np

class Segment:
    """represents a batch of line 1-D segments"""
    def __init__(self, middle, radius): # use other methods
        self.middle : np.ndarray = middle
        self.radius : np.ndarray = radius
        
    @staticmethod
    def from_x1x2(*, x1x2 : np.ndarray  = None, x1 = None, x2 = None) -> 'Segment':
        if x1x2 is not None:
            x1 = x1x2[:,0]
            x2 = x1x2[:,1]
        else:
            assert x1 is not None and x2 is not None

        assert (x1 <= x2).all()
        
        mid = (x2 + x1)/2
        rad = (x2 - x1)/2
        return Segment(mid, rad)
    
    def to_x1x2(self) -> np.ndarray:
        return np.stack([self.x1(), self.x2()], axis=1)

    @staticmethod
    def from_midrad(mid, rad) -> 'Segment':
        assert (rad >= 0).all()
        return Segment(mid, rad)
        
    def mid(self) -> np.ndarray:
        return self.middle
        
    def rad(self) -> np.ndarray:
        return self.radius
    
    def x1(self) -> np.ndarray:
        return self.middle - self.radius
        
    def x2(self) -> np.ndarray:
        return self.middle + self.radius
        
    def clip(self, minx, maxx) -> 'Segment':
        minx = np.array(minx)
        maxx = np.array(maxx)
        assert (maxx >= minx).all()

        newx1 = np.clip(self.x1(), minx, None)
        newx2 = np.clip(self.x2(), None, maxx)
        return Segment.from_x1x2(x1=newx1, x2=newx2)
        
    def fits(self, minx=None, maxx=None):
        if minx is not None:
            c1 = self.x1() >= minx
        else:
            c1 = True
        
        if maxx is not None:
            c2 = self.x2() <= maxx
        else:
            c2 = True
        
        return (c1 & c2).all()
        
    def length(self):
        return 2*self.rad()
    
    def best_seg(self, new_len, minx, maxx) -> 'Segment':
        """ forms a new segment `newseg` with following properties
        
            ## hard constraints
            newseg.length() == min(new_len, maxx - minx)
            minx <= newseg.x1() <= nnewseg.x2() <= maxx

            # newseg.intersect(self) is maximal
            
            ideally newseg.mid() == self.mid(), 
                but this isn't always possible (eg segment already near edge)
                so we aim to minimize |newseg.mid() - self.mid()|
            
            note new_len can be smaller or bigger than before.

        """
        minx = np.array(minx)
        maxx = np.array(maxx)
        assert (maxx >= minx).all()
        new_len = np.array(new_len)

        assert self.fits(minx, maxx)
        assert (new_len <= (maxx - minx)).all()
        
        raw_seg = Segment.from_midrad(self.mid(), new_len/2.)
        left_excess = np.clip(minx - raw_seg.x1(), 0, None)
        right_excess = np.clip(raw_seg.x2() - maxx, 0, None)

        assert (~((left_excess > 0) & (right_excess > 0))).all() # no excess could be both sides
        
        best_seg = Segment.from_midrad(mid=raw_seg.mid() + left_excess - right_excess, rad=raw_seg.rad())
        return best_seg

import pandas as pd
# from IPython.display import HTML, SVG
import shapely

def _as_polygons(df):
    df = df.assign(y1=-df.y1, y2=-df.y2) # reflect y-direction bc. svg 0,0 is bottom right
    return df[['x1', 'y1', 'x2', 'y2']].apply(lambda x : shapely.geometry.box(*tuple(x)), axis=1)

import math

class BoxBatch:
    """represents a batch of rectangular boxes within a w x h image"""
    def __init__(self, xseg, yseg):
        self.xseg : Segment = xseg
        self.yseg : Segment = yseg
        
    @staticmethod
    def from_xyxy(xyxy : np.ndarray) -> 'BoxBatch':
        xseg = Segment.from_x1x2(x1x2=xyxy[:,[0,2]])
        yseg = Segment.from_x1x2(x1x2=xyxy[:,[1,3]])
        return BoxBatch(xseg, yseg)

    @staticmethod
    def from_dataframe(df : pd.DataFrame, xyxy_columns=['x1', 'y1', 'x2', 'y2']):
        std_xyxy = ['x1', 'y1', 'x2', 'y2']
        tdict= dict(zip(xyxy_columns, std_xyxy))
        boxcols = df[xyxy_columns].rename(tdict, axis=1)
        return BoxBatch.from_xyxy(boxcols.values)

    def to_xyxy(self) -> np.ndarray:
        return np.stack([self.x1(), self.y1(), self.x2(), self.y2()], axis=1)

    def to_dataframe(self) -> pd.DataFrame:
        xyxy = self.to_xyxy()
        return pd.DataFrame({'x1':xyxy[:,0], 'y1':xyxy[:,1], 'x2':xyxy[:,2], 'y2':xyxy[:,3]})

    def __repr__(self) -> str:
        return self.to_dataframe().__repr__()

    def _repr_html_(self) -> str:
        df =  self.to_dataframe()
        polygons = _as_polygons(df) 
        df = df.assign(shape=polygons)
        styled = df.style.format({'shape':lambda shp: shp._repr_svg_()} , escape="html")
        return styled._repr_html_()

    def x1(self):
        return self.xseg.x1()
    
    def x2(self):
        return self.xseg.x2()
    
    def y1(self):
        return self.yseg.x1()
    
    def y2(self):
        return self.yseg.x2()
    
    def height(self):
        return self.yseg.length()
    
    def width(self):
        return self.xseg.length()
    
    def best_square_box(self, xmax=math.inf, ymax=math.inf):
        """ gets the square box that fits within bounds, overlaps as much as possible with box, 
            and is as near the center as possible"""

        max_container = np.minimum(xmax, ymax)
        max_side = np.maximum(self.height(), self.width())
        target_size = np.minimum(max_side, max_container)
        new_yseg = self.yseg.best_seg(target_size, minx=0, maxx=ymax)
        new_xseg = self.xseg.best_seg(target_size, minx=0, maxx=xmax)
        return BoxBatch(new_xseg, new_yseg)


class BoundingBoxBatch(BoxBatch):
    """represents box batch in the context of a larger image  of size  w, h
    """
    def __init__(self, xseg, yseg, im_width, im_height):
        super().__init__(xseg, yseg)
        self.im_width : np.ndarray = np.array(im_width)
        self.im_height : np.ndarray = np.array(im_height)

    @staticmethod
    def from_dataframe(df, xyxy_columns=['x1', 'y1', 'x2', 'y2', 'im_height', 'im_width']) -> 'BoundingBoxBatch':
        std_xyxy = ['x1', 'y1', 'x2', 'y2', 'im_height', 'im_width']
        tdict= dict(zip(xyxy_columns, std_xyxy))
        boxcols = df[xyxy_columns].rename(tdict, axis=1)
        bb = BoxBatch.from_dataframe(boxcols)
        return BoundingBoxBatch(bb.xseg, bb.yseg, im_width=boxcols['im_width'].values, 
                                    im_height=boxcols['im_height'].values)
        
    def to_dataframe(self) -> pd.DataFrame:
        df = super().to_dataframe()
        return df.assign(im_width=self.im_width, im_height=self.im_height)

    def _repr_html_(self) -> str:
        df =  self.to_dataframe()
        box_polygons = _as_polygons(df) # box polygons
        container_polygons = _as_polygons(pd.DataFrame({'x1':0, 'y1':0, 'x2':df['im_width'], 'y2':df['im_height']}))
        geoms = [shapely.geometry.GeometryCollection([bx, cont.boundary]) for (bx,cont) in zip(box_polygons, container_polygons)]
        df = df.assign(shape=geoms)
        styled = df.style.format({'shape':lambda shp: shp._repr_svg_()} , escape="html")
        return styled._repr_html_()

    def best_square_box(self) -> 'BoundingBoxBatch':
        bb = super().best_square_box(xmax=self.im_width, ymax=self.im_height)
        return BoundingBoxBatch(bb.xseg, bb.yseg, self.im_width, self.im_height)
