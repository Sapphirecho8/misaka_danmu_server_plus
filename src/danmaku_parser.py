import logging
import re
from typing import Dict, List
from xml.etree import ElementTree

from .utils import clean_xml_string

logger = logging.getLogger(__name__)

def parse_dandan_xml_to_comments(xml_content: str) -> List[Dict]:
    """
    Parses dandanplay-style XML content into a list of comment dictionaries.
    """
    comments = []
    try:
        # 关键修复：在解析之前，先清理XML内容，移除所有非法字符。
        # 这可以防止因弹幕内容包含无效控制字符（如退格符）而导致的解析失败。
        xml_content = clean_xml_string(xml_content)
        # Remove any XML declaration that might cause issues
        xml_content = re.sub(r'<\?xml.*?\?>', '', xml_content, count=1).strip()
        root = ElementTree.fromstring(xml_content)
        for comment_node in root.findall('d'):
            try:
                p_attr = comment_node.attrib.get('p', '0,1,25,16777215,0,0,0,0')
                text = comment_node.text or ''
                
                # Dandanplay format: p="弹幕出现时间,弹幕模式,弹幕颜色,发送者UID..."
                # We only care about time, mode, and color for our internal format.
                parts = p_attr.split(',')
                time_sec = float(parts[0]) if parts else 0.0
                # The last part of the 'p' attribute is the comment ID (cid)
                comment_id = int(parts[7]) if len(parts) > 7 else 0
                
                # Our internal format uses 'p' for params and 'm' for message.
                # We can just store the original 'p' attribute.
                comment_dict = {
                    'p': p_attr,
                    'm': text,
                    't': time_sec,  # For sorting/filtering if needed
                    'cid': comment_id
                }
                comments.append(comment_dict)
            except (IndexError, ValueError) as e:
                logger.warning(f"Skipping malformed comment node: {ElementTree.tostring(comment_node, 'unicode')}. Error: {e}")
                continue
    except ElementTree.ParseError as e:
        logger.error(f"Failed to parse XML content: {e}")
        # Return empty list if XML is invalid
        return []
    
    return comments
