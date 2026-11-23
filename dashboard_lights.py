"""Build the dashboard lighting snapshot from the existing HA state batch."""


ROOM_LIGHTS = {
    "br": (
        "light.yeelink_cn_1005464493_ml9_s_2_light",
        "light.yeelink_cn_1006211385_ml9_s_2_light",
        "light.yeelink_cn_1157130165_ml1_s_2",
        "light.yeelink_cn_1157130135_ml1_s_2",
        "light.yeelink_cn_1041253785_ml1_s_2_2",
        "light.yeelink_cn_1041277545_ml1_s_2_2",
        "light.yeelink_cn_1086993553_ml9_s_2_light",
        "light.yeelink_cn_1006129044_ml9_s_2_light",
    ),
    "st": (
        "light.yeelink_cn_360178505_lamp7_s_2_light",
        "light.lemesh_cn_1099619050_wy0c15_s_2_light",
    ),
    "lr": (
        "light.yeelink_cn_1009486140_ml9_s_2_light",
        "light.yeelink_cn_1008203146_ml9_s_2_light",
        "light.yeelink_cn_1009487498_ml9_s_2_light",
        "light.yeelink_cn_1009487199_ml9_s_2_light",
        "light.yeelink_cn_1008204232_ml9_s_2_light",
        "light.yeelink_cn_1008204547_ml9_s_2_light",
        "light.yeelink_cn_1009486591_ml9_s_2_light",
        "light.yeelink_cn_1008204661_ml9_s_2_light",
        "light.yeelink_cn_1009473309_ml9_s_2_light",
        "light.yeelink_cn_1008203664_ml9_s_2_light",
        "light.yeelink_cn_1009371931_ml9_s_2_light",
        "light.yeelink_cn_1009383549_ml9_s_2_light",
        "light.yeelink_cn_1009384148_ml9_s_2_light",
        "light.yeelink_cn_557098462_lamp9_s_2_light",
    ),
    "dr": (
        "light.lemesh_cn_1094870431_wy0c09_s_2_2",
        "light.yeelink_cn_1085410328_ml9_s_2_light",
        "light.yeelink_cn_1008616461_ml9_s_2_light",
        "light.lemesh_cn_1086635471_wy0c09_s_2_2",
    ),
    "sb": (
        "light.yeelink_cn_1157128117_ml1_s_2",
        "light.yeelink_cn_1157132083_ml1_s_2",
        "light.yeelink_cn_1085410138_ml9_s_2_light",
        "light.yeelink_cn_1085434376_ml9_s_2_light",
        "light.yeelink_cn_1003908131_ml9_s_2_light",
        "light.yeelink_cn_1085406358_ml9_s_2_light",
    ),
    "nb": (
        "light.yeelink_cn_1008021868_ml9_s_2_light",
        "light.yeelink_cn_1008256857_ml9_s_2_light",
    ),
    "kt": (
        "light.yeelink_cn_1085408128_ml9_s_2_light",
        "light.yeelink_cn_1085407519_ml9_s_2_light",
        "light.yeelink_cn_1008023005_ml9_s_2_light",
        "light.yeelink_cn_1008209843_ml9_s_2_light",
        "light.yeelink_cn_1005669369_ml9_s_2_light",
        "light.lemesh_cn_1100299749_wy0c15_s_2_light",
    ),
    "en": (
        "light.yeelink_cn_1085407781_ml9_s_2_light",
        "light.yeelink_cn_1006129158_ml9_s_2_light",
        "light.yeelink_cn_1093826704_ml9_s_2_light",
        "light.lemesh_cn_1085588800_wy0c08_s_2_light",
        "light.lemesh_cn_1121245207_wy0c15_s_2_light",
    ),
    "cr": (
        "light.yeelink_cn_1004208324_ml9_s_2_light",
        "light.yeelink_cn_1005436237_ml9_s_2_light",
        "light.yeelink_cn_1005668361_ml9_s_2_light",
        "light.yeelink_cn_1085373729_ml9_s_2_light",
    ),
    "bath": (
        "light.linp_cn_1131362725_lp1bc_s_2_light",
        "light.yeelink_cn_1085406943_ml9_s_2_light",
        "light.yeelink_cn_647044298_v6_s_2_light",
    ),
    "gb": (
        "light.yeelink_cn_1008208709_ml9_s_2_light",
    ),
}


def light_snapshot(states: dict, generated_at: int) -> dict:
    return {
        "generated_at": generated_at,
        "lights": {
            room: [
                {"id": entity, "state": states.get(entity, "unknown"), "name": entity}
                for entity in entities
            ]
            for room, entities in ROOM_LIGHTS.items()
        },
    }
