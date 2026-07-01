import argparse
from converter import *

# 创建解析器
parser = argparse.ArgumentParser(description='单位换算器')

# 添加命令行参数
parser.add_argument('value', type=float, help='要转换的数值')
parser.add_argument('from_unit', type=str, choices=['m', 'cm', 'ft', 'in'], help='原始单位')
parser.add_argument('to_unit', type=str, choices=['m', 'cm', 'ft', 'in'], help='目标单位')

# 解析参数
args = parser.parse_args()

# 转换逻辑
if args.from_unit == 'm':
    if args.to_unit == 'cm':
        result = meters_to_centimeters(args.value)
    elif args.to_unit == 'ft':
        result = meters_to_feet(args.value)
    elif args.to_unit == 'in':
        result = meters_to_inches(args.value)
    else:
        result = args.value
elif args.from_unit == 'cm':
    if args.to_unit == 'm':
        result = centimeters_to_meters(args.value)
    elif args.to_unit == 'ft':
        result = centimeters_to_feet(args.value)
    elif args.to_unit == 'in':
        result = centimeters_to_inches(args.value)
    else:
        result = args.value
elif args.from_unit == 'ft':
    if args.to_unit == 'm':
        result = feet_to_meters(args.value)
    elif args.to_unit == 'cm':
        result = feet_to_centimeters(args.value)
    elif args.to_unit == 'in':
        result = feet_to_inches(args.value)
    else:
        result = args.value
elif args.from_unit == 'in':
    if args.to_unit == 'm':
        result = inches_to_meters(args.value)
    elif args.to_unit == 'cm':
        result = inches_to_centimeters(args.value)
    elif args.to_unit == 'ft':
        result = inches_to_feet(args.value)
    else:
        result = args.value

# 输出结果
print(f'{args.value} {args.from_unit} = {result} {args.to_unit}')